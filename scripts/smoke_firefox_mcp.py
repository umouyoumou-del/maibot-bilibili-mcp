"""真实通过 Firefox MCP 读取一轮B站数据的冒烟测试。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from firefox_bilibili_mcp import FirefoxBilibiliMCPClient  # noqa: E402


def preview(payload: dict, limit: int = 1600) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return text[:limit] + ("..." if len(text) > limit else "")


async def main() -> int:
    parser = argparse.ArgumentParser(description="通过 Firefox MCP 冒烟测试刷B站数据链路")
    parser.add_argument("--url", default="http://127.0.0.1:21082/mcp", help="Browser Kit HTTP MCP 地址")
    parser.add_argument("--keyword", default="MaiBot", help="搜索关键词")
    args = parser.parse_args()

    client = FirefoxBilibiliMCPClient(args.url, call_timeout_seconds=120, page_load_timeout=25)
    try:
        async with client.connect() as browser:
            search = await browser.call_tool("bili_search", {"keyword": args.keyword, "num": 3, "order": "pubdate"})
            print("=== Firefox MCP / bili_search ===")
            print(preview(search))
            videos = search.get("videos") or []
            if not videos:
                print("没有搜索到视频。")
                return 1
            bvid = str(videos[0].get("bvid") or "")
            info = await browser.call_tool("bili_video_info", {"bvid": bvid})
            print("\n=== Firefox MCP / bili_video_info ===")
            print(preview(info))
            subtitle = await browser.call_tool("bili_subtitle", {"bvid": bvid})
            print("\n=== Firefox MCP / bili_subtitle ===")
            print(preview(subtitle))
            comments = await browser.call_tool(
                "bili_comments",
                {"bvid": bvid, "num": 3, "sort": "hot", "include_replies": True},
            )
            print("\n=== Firefox MCP / bili_comments ===")
            print(preview(comments))
        print("\nFirefox MCP smoke test passed.")
        return 0
    except Exception as exc:
        print(f"Firefox MCP smoke test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
