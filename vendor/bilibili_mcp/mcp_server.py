#!/usr/bin/env python3
"""只读 Bilibili MCP Server。

提供 MaiBot 刷B站所需的四个工具：
- ``bili_search``：关键词搜索视频
- ``bili_video_info``：读取视频详情
- ``bili_subtitle``：读取公开/AI 字幕原文
- ``bili_comments``：读取热门评论

本文件基于 AGI_Ananas/bilibili-mcp 的 MIT 实现裁剪而来，并额外支持在
没有登录凭证时以匿名身份读取公开数据。完整许可见同目录 LICENSE。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import aiohttp
from bilibili_api import Credential, comment, search, video
from bilibili_api.utils import network
from bilibili_api.utils.network import Api
from mcp.server.fastmcp import FastMCP

CREDENTIAL_FILE = Path(
    os.environ.get("BILI_CREDENTIAL_FILE", str(Path(__file__).with_name("bili_credential.json")))
).expanduser()
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("BILI_REQUEST_TIMEOUT", "15") or "15")
if user_agent := os.environ.get("BILI_USER_AGENT", "").strip():
    network.HEADERS["User-Agent"] = user_agent

mcp = FastMCP("bilibili-readonly-mcp")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def load_credential() -> Credential:
    """从环境指定的 JSON 文件加载凭证；不存在或损坏时回退到匿名凭证。"""

    if not CREDENTIAL_FILE.is_file():
        return Credential()

    try:
        data = json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[bili-mcp] 凭证文件读取失败，将使用匿名访问: {exc}", file=sys.stderr)
        return Credential()

    if not isinstance(data, dict):
        return Credential()

    return Credential(
        sessdata=str(data.get("sessdata") or ""),
        bili_jct=str(data.get("bili_jct") or ""),
        buvid3=str(data.get("buvid3") or ""),
        buvid4=str(data.get("buvid4") or ""),
        dedeuserid=str(data.get("dedeuserid") or ""),
        ac_time_value=str(data.get("ac_time_value") or ""),
    )


def _dump(payload: dict[str, Any]) -> str:
    """以 UTF-8 友好的方式序列化返回值。"""

    return json.dumps(payload, ensure_ascii=False)


def _clean_title(value: Any) -> str:
    """去除搜索结果里的关键词高亮标签。"""

    return _HTML_TAG_RE.sub("", str(value or "")).strip()


def _normalize_subtitle_target(subtitles: list[dict[str, Any]]) -> dict[str, Any] | None:
    """优先选择中文 AI 字幕，其次任意中文字幕，最后退回第一条。"""

    for language in ("ai-zh", "zh-CN", "zh-Hans", "zh"):
        for subtitle in subtitles:
            if str(subtitle.get("lan") or "") == language:
                return subtitle
    for language in ("ai-zh", "zh-CN", "zh-Hans", "zh"):
        for subtitle in subtitles:
            if str(subtitle.get("lan") or "").startswith(language):
                return subtitle
    return subtitles[0] if subtitles else None


async def _get_video_with_info(bvid: str) -> tuple[video.Video, dict[str, Any], Credential]:
    """构造视频对象并读取基础信息。"""

    normalized_bvid = str(bvid or "").strip()
    if not normalized_bvid:
        raise ValueError("bvid 不能为空")

    credential = load_credential()
    video_obj = video.Video(bvid=normalized_bvid, credential=credential)
    info = await video_obj.get_info()
    return video_obj, info, credential


@mcp.tool()
async def bili_search(keyword: str, num: int = 10, order: str = "totalrank") -> str:
    """搜索 B站视频。

    Args:
        keyword: 搜索关键词。
        num: 返回数量，最大 50。
        order: totalrank=综合，click=播放量，pubdate=最新，dm=弹幕。
    """

    normalized_keyword = str(keyword or "").strip()
    if not normalized_keyword:
        return _dump({"error": "keyword 不能为空", "videos": []})

    order_map = {
        "totalrank": search.OrderVideo.TOTALRANK,
        "click": search.OrderVideo.CLICK,
        "pubdate": search.OrderVideo.PUBDATE,
        "dm": search.OrderVideo.DM,
    }
    normalized_num = max(1, min(int(num or 10), 50))
    try:
        result = await search.search_by_type(
            keyword=normalized_keyword,
            search_type=search.SearchObjectType.VIDEO,
            page=1,
            order_type=order_map.get(str(order or ""), search.OrderVideo.TOTALRANK),
        )
    except Exception as exc:
        return _dump({"keyword": normalized_keyword, "error": f"{type(exc).__name__}: {exc}", "videos": []})

    videos: list[dict[str, Any]] = []
    for item in result.get("result", [])[:normalized_num]:
        videos.append(
            {
                "bvid": str(item.get("bvid") or ""),
                "aid": item.get("aid", 0),
                "title": _clean_title(item.get("title")),
                "author": str(item.get("author") or ""),
                "play": item.get("play", 0),
                "review": item.get("review", 0),
                "danmaku": item.get("video_review", 0),
                "duration": str(item.get("duration") or ""),
                "description": _clean_title(item.get("description"))[:300],
                "pubdate": item.get("pubdate", 0),
                "url": f"https://www.bilibili.com/video/{item.get('bvid', '')}",
            }
        )

    return _dump({"keyword": normalized_keyword, "count": len(videos), "videos": videos})


@mcp.tool()
async def bili_video_info(bvid: str) -> str:
    """读取视频标题、简介、UP 主与播放数据。"""

    try:
        _, info, _ = await _get_video_with_info(bvid)
    except Exception as exc:
        return _dump({"bvid": str(bvid or ""), "error": f"{type(exc).__name__}: {exc}"})

    stat = info.get("stat") or {}
    pages = info.get("pages") or []
    return _dump(
        {
            "bvid": info.get("bvid", bvid),
            "aid": info.get("aid", 0),
            "title": info.get("title", ""),
            "description": info.get("desc", ""),
            "author": (info.get("owner") or {}).get("name", ""),
            "duration": info.get("duration", 0),
            "pubdate": info.get("pubdate", 0),
            "pages": len(pages),
            "tags": [item.get("tag_name", "") for item in info.get("tag") or [] if item.get("tag_name")],
            "stat": {
                "view": stat.get("view", 0),
                "danmaku": stat.get("danmaku", 0),
                "reply": stat.get("reply", 0),
                "favorite": stat.get("favorite", 0),
                "coin": stat.get("coin", 0),
                "like": stat.get("like", 0),
                "share": stat.get("share", 0),
            },
            "url": f"https://www.bilibili.com/video/{info.get('bvid', bvid)}",
        }
    )


@mcp.tool()
async def bili_subtitle(bvid: str) -> str:
    """读取视频公开字幕或 AI 字幕的完整文本。

    未配置 B站登录凭证时仍会尝试读取公开字幕；如果 B站返回
    ``need_login_subtitle=true``，会在结果中明确标注。
    """

    try:
        video_obj, info, credential = await _get_video_with_info(bvid)
        cid = int(info.get("cid") or 0)
        if not cid and info.get("pages"):
            cid = int((info["pages"][0] or {}).get("cid") or 0)
        if not cid:
            return _dump({"bvid": str(bvid or ""), "error": "无法获取 cid"})

        # 直接调用播放器信息接口，允许匿名读取公开字幕；bilibili-api
        # 的 Video.get_subtitle 会强制要求 SESSDATA，不适合匿名场景。
        api = Api(
            url="https://api.bilibili.com/x/player/wbi/v2",
            method="GET",
            verify=False,
            wbi=True,
            dm=True,
            credential=credential,
        )
        player_info = await api.update_params(
            aid=info.get("aid", 0),
            cid=cid,
            isGaiaAvoided=False,
            web_location=1315873,
        ).result
    except Exception as exc:
        return _dump({"bvid": str(bvid or ""), "error": f"{type(exc).__name__}: {exc}"})

    subtitle_info = player_info.get("subtitle") or {}
    subtitles = subtitle_info.get("subtitles") or []
    target = _normalize_subtitle_target(subtitles)
    if target is None:
        return _dump(
            {
                "bvid": str(bvid or ""),
                "title": info.get("title", ""),
                "message": "该视频没有公开字幕",
                "need_login": bool(player_info.get("need_login_subtitle")),
            }
        )

    subtitle_url = str(target.get("subtitle_url") or "")
    if subtitle_url.startswith("//"):
        subtitle_url = f"https:{subtitle_url}"
    if not subtitle_url:
        return _dump({"bvid": str(bvid or ""), "title": info.get("title", ""), "error": "字幕地址为空"})

    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(subtitle_url) as response:
                response.raise_for_status()
                subtitle_data = await response.json(content_type=None)
    except Exception as exc:
        return _dump({"bvid": str(bvid or ""), "title": info.get("title", ""), "error": f"字幕下载失败: {exc}"})

    texts = [str(item.get("content") or "").strip() for item in subtitle_data.get("body") or []]
    full_text = "\n".join(text for text in texts if text)
    return _dump(
        {
            "bvid": info.get("bvid", bvid),
            "title": info.get("title", ""),
            "language": target.get("lan_doc", ""),
            "segments": len(texts),
            "text": full_text,
        }
    )


@mcp.tool()
async def bili_comments(bvid: str, num: int = 30) -> str:
    """读取视频热门评论。"""

    try:
        _, info, credential = await _get_video_with_info(bvid)
        aid = int(info.get("aid") or 0)
        if not aid:
            return _dump({"bvid": str(bvid or ""), "error": "无法获取 aid"})

        normalized_num = max(1, min(int(num or 30), 100))
        comments: list[dict[str, Any]] = []
        fetch_error = ""
        page = 1
        while len(comments) < normalized_num:
            try:
                response = await comment.get_comments(
                    oid=aid,
                    type_=comment.CommentResourceType.VIDEO,
                    page_index=page,
                    order=comment.OrderType.LIKE,
                    credential=credential,
                )
            except Exception as exc:
                fetch_error = f"{type(exc).__name__}: {exc}"
                break

            replies = response.get("replies") or []
            if not replies:
                break

            for reply in replies:
                member = reply.get("member") or {}
                content = reply.get("content") or {}
                comment_item: dict[str, Any] = {
                    "rpid": reply.get("rpid", 0),
                    "user": member.get("uname", ""),
                    "content": content.get("message", ""),
                    "like": reply.get("like", 0),
                    "reply_count": reply.get("rcount", 0),
                    "time": reply.get("ctime", 0),
                }
                sub_replies = (reply.get("replies") or [])[:2]
                if sub_replies:
                    comment_item["top_replies"] = [
                        {
                            "user": (sub.get("member") or {}).get("uname", ""),
                            "content": (sub.get("content") or {}).get("message", ""),
                            "like": sub.get("like", 0),
                        }
                        for sub in sub_replies
                    ]
                comments.append(comment_item)
                if len(comments) >= normalized_num:
                    break

            page += 1
            await asyncio.sleep(0.3)
    except Exception as exc:
        return _dump({"bvid": str(bvid or ""), "error": f"{type(exc).__name__}: {exc}", "comments": []})

    payload: dict[str, Any] = {"bvid": str(bvid or ""), "count": len(comments), "comments": comments}
    if fetch_error:
        payload["error"] = fetch_error
    return _dump(payload)


if __name__ == "__main__":
    mcp.run(transport="stdio")
