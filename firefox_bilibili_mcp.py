"""通过 Firefox MCP（MCP Browser Kit）实现刷B站所需的工具接口。

这个适配器对上层暴露与 ``BilibiliMCPClient`` 相同的
``connect() -> connection.call_tool(name, arguments)`` 接口，因此
``BilibiliScrollService`` 的选片、模型回想和长期记忆逻辑无需改变。

实际数据来源：
- 用 Browser Kit 打开 B站搜索页并读取搜索结果卡片；
- 在 Firefox 的 B站页面上下文里用带登录 Cookie 的同步 XHR 调B站公开接口；
- 读取视频详情、播放器信息/字幕、热门评论。

只通过浏览器页面上下文访问数据，不再由插件自己启动 Bilibili MCP Server。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

_BVID_RE = re.compile(r"^BV[0-9A-Za-z]+$")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


class FirefoxMCPError(RuntimeError):
    """Firefox MCP 调用失败。"""


class BrowserKitConnection:
    """对 Browser Kit MCP 的薄封装，负责统一解析 structured/text 返回。"""

    def __init__(self, session: ClientSession, call_timeout_seconds: float) -> None:
        self._session = session
        self._call_timeout_seconds = call_timeout_seconds

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用 Browser Kit 工具并返回结构化数据。"""

        timeout = timedelta(seconds=max(1.0, self._call_timeout_seconds))
        try:
            result = await self._session.call_tool(name, arguments or {}, read_timeout_seconds=timeout)
        except Exception as exc:
            raise FirefoxMCPError(f"Firefox MCP 工具 {name} 调用失败: {type(exc).__name__}: {exc}") from exc

        if bool(getattr(result, "isError", False)):
            raise FirefoxMCPError(self._text_content(result) or f"Firefox MCP 工具 {name} 返回错误")

        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict) and structured:
            return self._validate_payload(structured, name)

        text = self._text_content(result)
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FirefoxMCPError(f"Firefox MCP 工具 {name} 返回了非 JSON 内容") from exc
        if not isinstance(payload, dict):
            return {"result": payload}
        return self._validate_payload(payload, name)

    async def open_tab(self, browser_id: str, window_id: str, url: str) -> dict[str, str]:
        """打开新标签页，返回 browserId/windowId/tabId。"""

        payload = await self.call_tool(
            "openTab",
            {"browserId": browser_id, "windowId": window_id, "url": url},
        )
        value = payload.get("value")
        if not isinstance(value, dict):
            raise FirefoxMCPError(f"openTab 返回格式异常: {payload}")
        return {
            "browserId": str(value.get("browserId") or browser_id),
            "windowId": str(value.get("windowId") or window_id),
            "tabId": str(value.get("tabId") or ""),
        }

    async def close_tab(self, browser_id: str, window_id: str, tab_id: str) -> None:
        """关闭标签页；失败只记录，不中断主流程。"""

        try:
            await self.call_tool(
                "closeTab",
                {
                    "browserId": browser_id,
                    "windowId": window_id,
                    "tabId": tab_id,
                },
            )
        except Exception:
            pass

    async def invoke_js(self, browser_id: str, window_id: str, tab_id: str, body: str) -> Any:
        """在指定标签页执行同步 JS 函数体并返回结果。"""

        payload = await self.call_tool(
            "invokeJsFn",
            {
                "browserId": browser_id,
                "windowId": window_id,
                "tabId": tab_id,
                "fnBodyCode": body,
            },
        )
        value = payload.get("value")
        if isinstance(value, dict) and "result" in value:
            return value["result"]
        return value

    @staticmethod
    def _text_content(result: Any) -> str:
        return "\n".join(
            str(getattr(item, "text", "") or "")
            for item in (getattr(result, "content", None) or [])
            if getattr(item, "text", None) is not None
        ).strip()

    @staticmethod
    def _validate_payload(payload: dict[str, Any], tool_name: str) -> dict[str, Any]:
        if payload.get("ok") is False:
            reason = payload.get("reason") or payload.get("message") or payload.get("error") or "未知错误"
            raise FirefoxMCPError(f"Firefox MCP 工具 {tool_name} 失败: {reason}")
        return payload


@dataclass(slots=True)
class _ViewData:
    bvid: str
    aid: int
    cid: int
    title: str
    description: str
    author: str
    duration: int
    pubdate: int
    view: int
    danmaku: int
    reply: int
    favorite: int
    coin: int
    like: int
    share: int
    pages: int


@dataclass(slots=True)
class FirefoxBilibiliConnection:
    """在单个刷B站轮次内复用的 Firefox/B站页面连接。"""

    kit: BrowserKitConnection
    browser_id: str
    window_id: str
    page_load_timeout: float = 20.0
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("maibot.firefox.bilibili"))
    worker_tab_id: str = ""
    _view_cache: dict[str, _ViewData] = field(default_factory=dict)

    async def start(self) -> None:
        """打开一个 B站首页 worker 标签，用于后续带 Cookie 的接口调用。"""

        worker = await self.kit.open_tab(
            self.browser_id,
            self.window_id,
            "https://www.bilibili.com/",
        )
        self.worker_tab_id = worker["tabId"]
        await self._wait_tab_url(self.worker_tab_id, ("bilibili.com",))
        await self._wait_page_ready(self.worker_tab_id, require_search_cards=False)

    async def close(self) -> None:
        """关闭 worker 标签。"""

        if self.worker_tab_id:
            await self.kit.close_tab(self.browser_id, self.window_id, self.worker_tab_id)
            self.worker_tab_id = ""

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """实现刷B站服务所需的 Bilibili MCP 兼容工具接口。"""

        args = arguments or {}
        if name == "bili_search":
            return await self._bili_search(
                keyword=str(args.get("keyword") or ""),
                num=int(args.get("num") or 10),
                order=str(args.get("order") or "totalrank"),
            )
        if name == "bili_video_info":
            return await self._bili_video_info(str(args.get("bvid") or ""))
        if name == "bili_subtitle":
            return await self._bili_subtitle(str(args.get("bvid") or ""))
        if name == "bili_comments":
            return await self._bili_comments(
                str(args.get("bvid") or ""),
                int(args.get("num") or 30),
            )
        return {"error": f"未知的 Firefox 刷B站工具: {name}"}

    async def _bili_search(self, *, keyword: str, num: int, order: str) -> dict[str, Any]:
        normalized_keyword = keyword.strip()
        if not normalized_keyword:
            return {"error": "keyword 不能为空", "videos": []}
        normalized_num = max(1, min(num, 50))
        order_map = {"totalrank": "totalrank", "click": "click", "pubdate": "pubdate", "dm": "dm"}
        search_url = (
            "https://search.bilibili.com/all?keyword="
            f"{urllib.parse.quote(normalized_keyword)}&order={order_map.get(order, 'totalrank')}"
        )
        tab = await self.kit.open_tab(self.browser_id, self.window_id, search_url)
        tab_id = tab["tabId"]
        try:
            await self._wait_tab_url(tab_id, ("search.bilibili.com", "bilibili.com"))
            await self._wait_page_ready(tab_id, require_search_cards=True)
            videos = await self._extract_search_cards(tab_id, normalized_num)
            return {"keyword": normalized_keyword, "count": len(videos), "videos": videos}
        except Exception as exc:
            return {"keyword": normalized_keyword, "error": f"{type(exc).__name__}: {exc}", "videos": []}
        finally:
            await self.kit.close_tab(self.browser_id, self.window_id, tab_id)

    async def _bili_video_info(self, bvid: str) -> dict[str, Any]:
        try:
            view = await self._get_view(bvid)
        except Exception as exc:
            return {"bvid": bvid, "error": f"{type(exc).__name__}: {exc}"}
        return self._view_to_info_payload(view)

    async def _bili_subtitle(self, bvid: str) -> dict[str, Any]:
        try:
            view = await self._get_view(bvid)
        except Exception as exc:
            return {"bvid": bvid, "error": f"{type(exc).__name__}: {exc}"}

        try:
            player = await self._xhr_json(f"https://api.bilibili.com/x/player/v2?aid={view.aid}&cid={view.cid}")
        except Exception as exc:
            return {"bvid": bvid, "title": view.title, "error": f"播放器接口读取失败: {exc}"}

        if int(player.get("code") or 0) != 0:
            return {
                "bvid": bvid,
                "title": view.title,
                "error": f"播放器接口返回 {player.get('code')}: {player.get('message') or ''}",
            }

        data = player.get("data") or {}
        subtitle_info = data.get("subtitle") or {}
        subtitles = subtitle_info.get("subtitles") or []
        target = self._pick_subtitle(subtitles)
        if target is None:
            return {
                "bvid": bvid,
                "title": view.title,
                "message": "该视频没有公开字幕",
                "need_login": bool(subtitle_info.get("need_login_subtitle")),
            }

        subtitle_url = str(target.get("subtitle_url") or "")
        if subtitle_url.startswith("//"):
            subtitle_url = f"https:{subtitle_url}"
        if not subtitle_url:
            return {"bvid": bvid, "title": view.title, "error": "字幕地址为空"}

        try:
            subtitle_payload = await self._xhr_json(subtitle_url)
        except Exception as exc:
            return {"bvid": bvid, "title": view.title, "error": f"字幕下载失败: {exc}"}

        texts = [
            str(item.get("content") or "").strip()
            for item in (subtitle_payload.get("body") or [])
            if str(item.get("content") or "").strip()
        ]
        return {
            "bvid": bvid,
            "title": view.title,
            "language": str(target.get("lan_doc") or ""),
            "segments": len(texts),
            "text": "\n".join(texts),
        }

    async def _bili_comments(self, bvid: str, num: int) -> dict[str, Any]:
        try:
            view = await self._get_view(bvid)
        except Exception as exc:
            return {"bvid": bvid, "error": f"{type(exc).__name__}: {exc}", "comments": []}

        normalized_num = max(1, min(num, 100))
        try:
            payload = await self._xhr_json(
                f"https://api.bilibili.com/x/v2/reply?type=1&oid={view.aid}&sort=2&ps={normalized_num}&pn=1"
            )
        except Exception as exc:
            return {"bvid": bvid, "error": f"评论接口读取失败: {exc}", "comments": []}

        if int(payload.get("code") or 0) != 0:
            return {
                "bvid": bvid,
                "error": f"评论接口返回 {payload.get('code')}: {payload.get('message') or ''}",
                "comments": [],
            }

        comments: list[dict[str, Any]] = []
        for reply in ((payload.get("data") or {}).get("replies") or [])[:normalized_num]:
            member = reply.get("member") or {}
            content = reply.get("content") or {}
            item: dict[str, Any] = {
                "rpid": reply.get("rpid", 0),
                "user": member.get("uname", ""),
                "content": content.get("message", ""),
                "like": reply.get("like", 0),
                "reply_count": reply.get("rcount", 0),
                "time": reply.get("ctime", 0),
            }
            sub_replies = (reply.get("replies") or [])[:2]
            if sub_replies:
                item["top_replies"] = [
                    {
                        "user": (sub.get("member") or {}).get("uname", ""),
                        "content": (sub.get("content") or {}).get("message", ""),
                        "like": sub.get("like", 0),
                    }
                    for sub in sub_replies
                ]
            comments.append(item)

        return {"bvid": bvid, "count": len(comments), "comments": comments}

    async def _get_view(self, bvid: str) -> _ViewData:
        normalized_bvid = str(bvid or "").strip()
        if not _BVID_RE.fullmatch(normalized_bvid):
            raise FirefoxMCPError("bvid 格式不合法")
        if cached := self._view_cache.get(normalized_bvid):
            return cached

        payload = await self._xhr_json(f"https://api.bilibili.com/x/web-interface/view?bvid={normalized_bvid}")
        if int(payload.get("code") or 0) != 0:
            raise FirefoxMCPError(f"视频详情接口返回 {payload.get('code')}: {payload.get('message') or ''}")
        data = payload.get("data") or {}
        stat = data.get("stat") or {}
        pages = data.get("pages") or []
        page_data = pages[0] if pages else {}
        cid = int(data.get("cid") or page_data.get("cid") or 0)
        if not cid:
            raise FirefoxMCPError("无法获取 cid")
        view = _ViewData(
            bvid=str(data.get("bvid") or normalized_bvid),
            aid=int(data.get("aid") or 0),
            cid=cid,
            title=_HTML_TAG_RE.sub("", str(data.get("title") or "")).strip(),
            description=str(data.get("desc") or "").strip(),
            author=str((data.get("owner") or {}).get("name") or "").strip(),
            duration=int(data.get("duration") or 0),
            pubdate=int(data.get("pubdate") or 0),
            view=int(stat.get("view") or 0),
            danmaku=int(stat.get("danmaku") or 0),
            reply=int(stat.get("reply") or 0),
            favorite=int(stat.get("favorite") or 0),
            coin=int(stat.get("coin") or 0),
            like=int(stat.get("like") or 0),
            share=int(stat.get("share") or 0),
            pages=len(pages),
        )
        self._view_cache[normalized_bvid] = view
        return view

    def _view_to_info_payload(self, view: _ViewData) -> dict[str, Any]:
        return {
            "bvid": view.bvid,
            "aid": view.aid,
            "title": view.title,
            "description": view.description,
            "author": view.author,
            "duration": view.duration,
            "pubdate": view.pubdate,
            "pages": view.pages,
            "tags": [],
            "stat": {
                "view": view.view,
                "danmaku": view.danmaku,
                "reply": view.reply,
                "favorite": view.favorite,
                "coin": view.coin,
                "like": view.like,
                "share": view.share,
            },
            "url": f"https://www.bilibili.com/video/{view.bvid}",
        }

    async def _xhr_json(self, url: str) -> dict[str, Any]:
        """在 Firefox 的B站页面上下文执行同步 XHR，复用浏览器 Cookie。"""

        if not self.worker_tab_id:
            raise FirefoxMCPError("Firefox worker 标签页尚未初始化")
        js = (
            "try {"
            f" const xhr=new XMLHttpRequest(); xhr.open('GET',{json.dumps(url)},false);"
            " xhr.withCredentials=true; xhr.send(null);"
            " const data=JSON.parse(xhr.responseText);"
            " return {ok:true, status:xhr.status, data:data};"
            " } catch(e) { return {ok:false, error:String(e)}; }"
        )
        result = await self.kit.invoke_js(
            self.browser_id,
            self.window_id,
            self.worker_tab_id,
            js,
        )
        if not isinstance(result, dict) or not result.get("ok"):
            reason = result.get("error") if isinstance(result, dict) else result
            raise FirefoxMCPError(f"页面 XHR 失败: {reason}")
        payload = result.get("data")
        if not isinstance(payload, dict):
            raise FirefoxMCPError("页面 XHR 返回格式异常")
        return payload

    async def _extract_search_cards(self, tab_id: str, limit: int) -> list[dict[str, Any]]:
        body = rf"""
const parseCount = (text) => {{
  const match = String(text || '').trim().match(/([\d.]+)\s*([万亿]?)/);
  if (!match) return 0;
  const value = Number(match[1]);
  if (!Number.isFinite(value)) return 0;
  return Math.round(value * (match[2] === '亿' ? 100000000 : match[2] === '万' ? 10000 : 1));
}};
const parsePubdate = (text) => {{
  const now = Math.floor(Date.now() / 1000);
  let m;
  if (/刚刚/.test(text)) return now;
  if ((m = text.match(/(\d+)\s*分钟前/))) return now - Number(m[1]) * 60;
  if ((m = text.match(/(\d+)\s*小时前/))) return now - Number(m[1]) * 3600;
  if ((m = text.match(/(\d+)\s*天前/))) return now - Number(m[1]) * 86400;
  if (/昨天/.test(text)) return now - 86400;
  if (/前天/.test(text)) return now - 172800;
  const makeDate = (month, day) => {{
    const d = new Date(new Date().getFullYear(), month - 1, day);
    if (d.getTime() > Date.now()) d.setFullYear(d.getFullYear() - 1);
    return Math.floor(d.getTime() / 1000);
  }};
  if ((m = text.match(/(\d{{1,2}})-(\d{{1,2}})/))) return makeDate(Number(m[1]), Number(m[2]));
  if ((m = text.match(/(\d{{1,2}})月(\d{{1,2}})日/))) return makeDate(Number(m[1]), Number(m[2]));
  return 0;
}};
const cards = Array.from(document.querySelectorAll('.bili-video-card'));
const seen = new Set();
const out = [];
for (const card of cards) {{
  const anchor = card.querySelector('a[href*="/video/BV"]');
  if (!anchor) continue;
  const match = anchor.href.match(/BV[0-9A-Za-z]+/);
  if (!match || seen.has(match[0])) continue;
  seen.add(match[0]);
  const titleEl = card.querySelector('.bili-video-card__info--tit');
  const authorEl = card.querySelector('.bili-video-card__info--author');
  const stats = Array.from(card.querySelectorAll('.bili-video-card__stats--item')).map((el) => el.innerText.trim());
  const info = (card.querySelector('.bili-video-card__info') || {{}}).innerText || card.innerText || '';
  const durationMatch = String(card.innerText || '').match(/\b\d{{1,2}}:\d{{2}}(?::\d{{2}})?\b/);
  out.push({{
    bvid: match[0],
    aid: 0,
    title: titleEl ? titleEl.innerText.trim() : '',
    author: authorEl ? authorEl.innerText.trim() : '',
    play: parseCount(stats[0] || ''),
    danmaku: parseCount(stats[1] || ''),
    duration: durationMatch ? durationMatch[0] : '',
    description: '',
    pubdate: parsePubdate(info),
    review: 0,
    url: 'https://www.bilibili.com/video/' + match[0],
  }});
}}
return {{ready: document.readyState, title: document.title, videos: out.slice(0, {max(1, limit)})}};
"""
        result = await self.kit.invoke_js(self.browser_id, self.window_id, tab_id, body)
        if not isinstance(result, dict):
            raise FirefoxMCPError("搜索结果解析返回格式异常")
        videos = result.get("videos")
        if not isinstance(videos, list):
            raise FirefoxMCPError("搜索结果为空")
        return [item for item in videos if isinstance(item, dict)]

    async def _wait_tab_url(self, tab_id: str, expected_parts: tuple[str, ...]) -> None:
        """等待新标签页离开 about:blank 并进入目标站点。"""

        deadline = asyncio.get_running_loop().time() + max(1.0, self.page_load_timeout)
        last_url = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                context = await self.kit.call_tool("getContext", {})
                browsers = context.get("browsers") if isinstance(context, dict) else []
                for browser in browsers or []:
                    if not isinstance(browser, dict):
                        continue
                    for tab in browser.get("tabs") or []:
                        if not isinstance(tab, dict) or str(tab.get("id")) != str(tab_id):
                            continue
                        last_url = str(tab.get("url") or "")
                        if (
                            last_url
                            and not last_url.startswith("about:")
                            and any(part in last_url for part in expected_parts)
                        ):
                            # 等页面脚本可用，但不要无限阻塞。
                            await asyncio.sleep(0.6)
                            return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        raise FirefoxMCPError(f"等待 Firefox 标签页导航超时，当前 URL: {last_url or 'unknown'}")

    async def _wait_page_ready(self, tab_id: str, *, require_search_cards: bool) -> None:
        deadline = asyncio.get_running_loop().time() + max(1.0, self.page_load_timeout)
        last_error = ""
        while asyncio.get_running_loop().time() < deadline:
            try:
                check = await self.kit.invoke_js(
                    self.browser_id,
                    self.window_id,
                    tab_id,
                    (
                        "return {ready: document.readyState, "
                        "count: document.querySelectorAll('.bili-video-card').length, "
                        "title: document.title};"
                    ),
                )
                if isinstance(check, dict) and check.get("ready") == "complete":
                    if not require_search_cards or int(check.get("count") or 0) > 0:
                        return
            except Exception as exc:
                last_error = str(exc)
            await asyncio.sleep(0.8)
        detail = f"（{last_error}）" if last_error else ""
        raise FirefoxMCPError(f"等待 Firefox 页面加载超时{detail}")

    @staticmethod
    def _pick_subtitle(subtitles: Any) -> dict[str, Any] | None:
        if not isinstance(subtitles, list) or not subtitles:
            return None
        normalized = [item for item in subtitles if isinstance(item, dict)]
        for language in ("ai-zh", "zh-CN", "zh-Hans", "zh"):
            for subtitle in normalized:
                if str(subtitle.get("lan") or "") == language:
                    return subtitle
        for language in ("ai-zh", "zh-CN", "zh-Hans", "zh"):
            for subtitle in normalized:
                if str(subtitle.get("lan") or "").startswith(language):
                    return subtitle
        return normalized[0] if normalized else None


class FirefoxBilibiliMCPClient:
    """连接常驻 Browser Kit HTTP MCP 服务，并实现 Bilibili MCP 兼容接口。"""

    def __init__(
        self,
        mcp_url: str,
        *,
        call_timeout_seconds: float = 120.0,
        page_load_timeout: float = 20.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self.mcp_url = str(mcp_url or "").strip()
        self.call_timeout_seconds = max(5.0, float(call_timeout_seconds))
        self.page_load_timeout = max(5.0, float(page_load_timeout))
        self.logger = logger or logging.getLogger("maibot.firefox.bilibili")

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[FirefoxBilibiliConnection]:
        if not self.mcp_url:
            raise FirefoxMCPError("未配置 Firefox MCP URL")

        http_timeout = httpx.Timeout(self.call_timeout_seconds, connect=10.0)
        try:
            async with httpx.AsyncClient(timeout=http_timeout) as http_client:
                async with streamable_http_client(self.mcp_url, http_client=http_client) as (
                    read_stream,
                    write_stream,
                    _get_session_id,
                ):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=self.call_timeout_seconds),
                    ) as session:
                        await session.initialize()
                        kit = BrowserKitConnection(session, self.call_timeout_seconds)
                        context = await kit.call_tool("getContext", {})
                        browser_id, window_id = self._pick_browser_window(context)
                        connection = FirefoxBilibiliConnection(
                            kit=kit,
                            browser_id=browser_id,
                            window_id=window_id,
                            page_load_timeout=self.page_load_timeout,
                            logger=self.logger,
                        )
                        try:
                            await connection.start()
                            yield connection
                        finally:
                            await connection.close()
        except FirefoxMCPError:
            raise
        except Exception as exc:
            raise FirefoxMCPError(f"连接 Firefox MCP 失败（{self.mcp_url}）: {type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _pick_browser_window(context: dict[str, Any]) -> tuple[str, str]:
        browsers = context.get("browsers") if isinstance(context, dict) else None
        if not isinstance(browsers, list):
            raise FirefoxMCPError("Firefox MCP 未返回浏览器列表")
        online = [item for item in browsers if isinstance(item, dict) and item.get("status") == "online"]
        if not online:
            raise FirefoxMCPError("Firefox MCP 没有已连接的浏览器；请确认 Firefox 和 MCP Browser Kit 扩展已启动")
        browser = online[0]
        browser_id = str(browser.get("browserId") or "").strip()
        windows = browser.get("windows") if isinstance(browser.get("windows"), list) else []
        window = next((item for item in windows if isinstance(item, dict) and item.get("focused")), None)
        if window is None:
            window = next((item for item in windows if isinstance(item, dict)), None)
        if not browser_id or window is None:
            raise FirefoxMCPError("Firefox MCP 没有可用的浏览器窗口")
        window_id = str(window.get("id") or "").strip()
        if not window_id:
            raise FirefoxMCPError("Firefox MCP 窗口 ID 为空")
        return browser_id, window_id
