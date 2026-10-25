"""真实拉起只读 Bilibili MCP Server 并执行一次冒烟测试。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from bilibili_mcp_client import BilibiliMCPClient  # noqa: E402

MCP_SERVER_PATH = PROJECT_ROOT / "vendor" / "bilibili_mcp" / "mcp_server.py"


def _preview(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return text[:1200] + ("..." if len(text) > 1200 else "")


async def main() -> int:
    parser = argparse.ArgumentParser(description="冒烟测试 Bilibili MCP Server")
    parser.add_argument("--keyword", default="MaiBot", help="搜索关键词")
    args = parser.parse_args()

    client = BilibiliMCPClient(
        MCP_SERVER_PATH,
        tool_timeout_seconds=45,
        log_path=PROJECT_ROOT / "logs" / "mcp-smoke.log",
    )
    try:
        async with client.connect() as mcp:
            search_result = await mcp.call_tool(
                "bili_search",
                {"keyword": args.keyword, "num": 2, "order": "pubdate"},
            )
            print("=== bili_search ===")
            print(_preview(search_result))
            videos = search_result.get("videos") or []
            if not videos:
                print("没有搜索到视频，冒烟测试结束。")
                return 1

            bvid = str(videos[0].get("bvid") or "")
            info_result = await mcp.call_tool("bili_video_info", {"bvid": bvid})
            print("\n=== bili_video_info ===")
            print(_preview(info_result))

            subtitle_result = await mcp.call_tool("bili_subtitle", {"bvid": bvid})
            print("\n=== bili_subtitle ===")
            print(_preview(subtitle_result))

            comments_result = await mcp.call_tool(
                "bili_comments",
                {"bvid": bvid, "num": 3, "sort": "hot", "include_replies": True},
            )
            print("\n=== bili_comments ===")
            print(_preview(comments_result))
        print("\nMCP smoke test passed.")
        return 0
    except Exception as exc:
        print(f"MCP smoke test failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
