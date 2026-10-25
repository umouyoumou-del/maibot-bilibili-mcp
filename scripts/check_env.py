"""检查 MaiBot 插件开发环境是否可用。"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """执行环境与插件骨架检查，失败时返回非零退出码。"""

    sys.path.insert(0, str(PROJECT_ROOT))

    import aiohttp
    import httpx
    import maibot_sdk

    from plugin import BilibiliPlugin, create_plugin

    manifest = json.loads((PROJECT_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    plugin = create_plugin()
    component_names = sorted(component["name"] for component in plugin.get_components())
    default_config = plugin.get_default_config()

    assert isinstance(plugin, BilibiliPlugin)
    assert manifest["manifest_version"] == 2
    assert component_names == ["bili_recall", "bili_scroll"]
    assert default_config["plugin"]["enabled"] is True
    assert default_config["model"]["ingest_memory"] is True
    assert (PROJECT_ROOT / "vendor" / "bilibili_mcp" / "mcp_server.py").is_file()

    version = importlib.metadata.version
    print(f"Python     : {sys.version.split()[0]}")
    print(f"maibot-sdk : {maibot_sdk.__version__}")
    print(f"mcp        : {version('mcp')}")
    print(f"bilibili   : {version('bilibili-api-python')}")
    print(f"aiohttp    : {aiohttp.__version__}")
    print(f"httpx      : {httpx.__version__}")
    print(f"plugin id  : {manifest['id']}")
    print(f"version    : {manifest['version']}")
    print(f"components : {', '.join(component_names)}")
    print("Environment check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
