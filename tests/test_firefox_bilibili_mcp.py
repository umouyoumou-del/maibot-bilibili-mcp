"""Firefox MCP 适配器的纯离线单元测试。"""

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
