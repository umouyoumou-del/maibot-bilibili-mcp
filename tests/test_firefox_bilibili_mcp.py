"""Firefox MCP 适配器的纯离线单元测试。"""

from types import SimpleNamespace
from typing import Any

import pytest

from firefox_bilibili_mcp import FirefoxBilibiliConnection, FirefoxBilibiliMCPClient


def test_pick_browser_window_prefers_online_focused_window() -> None:
    context = {
        "browsers": [
            {"browserId": "offline", "status": "offline", "windows": [{"id": "1", "focused": True}]},
            {
                "browserId": "firefox-1",
                "status": "online",
                "windows": [
                    {"id": "background", "focused": False},
                    {"id": "focused", "focused": True},
                ],
            },
        ]
    }

    assert FirefoxBilibiliMCPClient._pick_browser_window(context) == ("firefox-1", "focused")


def test_pick_browser_window_raises_without_online_browser() -> None:
    context = {"browsers": [{"browserId": "offline", "status": "offline", "windows": []}]}

    try:
        FirefoxBilibiliMCPClient._pick_browser_window(context)
    except Exception as exc:
        assert "没有已连接的浏览器" in str(exc)
    else:
        raise AssertionError("expected FirefoxMCPError")


def test_pick_subtitle_prefers_chinese_ai_subtitle() -> None:
    subtitles = [
        {"lan": "en-US", "lan_doc": "English", "subtitle_url": "//example/en.json"},
        {"lan": "ai-zh", "lan_doc": "中文（自动生成）", "subtitle_url": "//example/zh.json"},
    ]

    target = FirefoxBilibiliConnection._pick_subtitle(subtitles)

    assert target is not None
    assert target["lan"] == "ai-zh"


@pytest.mark.asyncio
async def test_comments_paginate_sort_and_include_replies() -> None:
    """请求 25 条最新评论时应翻两页，并保留楼中楼回复。"""

    requested_urls: list[str] = []

    def make_reply(index: int) -> dict[str, Any]:
        return {
            "rpid": index,
            "member": {"uname": f"用户{index}"},
            "content": {"message": f"评论内容{index}"},
            "like": index,
            "rcount": 1,
            "ctime": 1792920000 + index,
            "replies": [
                {
                    "member": {"uname": f"回复者{index}"},
                    "content": {"message": f"楼中楼{index}"},
                    "like": 1,
                }
            ],
        }

    class StubConnection(FirefoxBilibiliConnection):
        async def _get_view(self, bvid: str) -> Any:
            del bvid
            return SimpleNamespace(aid=12345, reply=42)

        async def _xhr_json(self, url: str) -> dict[str, Any]:
            requested_urls.append(url)
            page = 2 if "next=83" in url else 1
            count = 5 if page == 2 else 20
            offset = 20 if page == 2 else 0
            return {
                "code": 0,
                "data": {
                    "replies": [make_reply(offset + index) for index in range(1, count + 1)],
                    "cursor": {"next": 83, "is_end": page == 2},
                },
            }

    connection = StubConnection(kit=object(), browser_id="firefox-1", window_id="window-1")
    result = await connection._bili_comments("BV1Test000001", 25, sort="new", include_replies=True)

    assert result["count"] == 25
    assert result["total"] == 42
    assert result["sort"] == "new"
    assert len(requested_urls) == 2
    assert "mode=2" in requested_urls[0]
    assert "next=0" in requested_urls[0]
    assert "next=83" in requested_urls[1]
    assert result["comments"][0]["top_replies"][0]["content"] == "楼中楼1"
