"""插件骨架、配置与 Manifest 检查。"""

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

from plugin import BilibiliPlugin, BilibiliPluginConfig, create_plugin


def test_create_plugin_returns_plugin_instance() -> None:
    plugin = create_plugin()

    assert isinstance(plugin, BilibiliPlugin)


def test_plugin_declares_command_and_tools() -> None:
    plugin = BilibiliPlugin()
    components = {component["name"]: component for component in plugin.get_components()}

    assert components["bili_scroll"]["type"].lower() == "command"
    assert components["bili_recall"]["type"].lower() == "tool"


def test_default_config_is_valid() -> None:
    plugin = BilibiliPlugin()
    default_config = plugin.get_default_config()
    validated_config = BilibiliPluginConfig.model_validate(default_config)

    assert validated_config.plugin.enabled is True
    assert validated_config.plugin.config_version == "0.6.0"
    assert validated_config.bilibili.request_timeout == 45.0
    assert validated_config.scroll.data_source == "firefox"
    assert validated_config.scroll.model_directed is True
    assert validated_config.scroll.model_select_candidates is True
    assert validated_config.scroll.use_persona is True
    assert validated_config.firefox.mcp_url == "http://127.0.0.1:21082/mcp"
    assert validated_config.scroll.keywords == ["MaiBot", "MaiMai", "AI Agent"]
    assert validated_config.scroll.max_age_days == 7
    assert validated_config.scroll.comments_per_video == 12
    assert validated_config.scroll.comment_sort == "hot"
    assert validated_config.scroll.include_comment_replies is True
    assert validated_config.scroll.comment_digest_max_items == 4
    assert validated_config.model.ingest_memory is True


@pytest.mark.asyncio
async def test_auto_task_is_not_started_by_default() -> None:
    plugin = BilibiliPlugin()
    plugin.set_plugin_config(plugin.get_default_config())

    class FakeContext:
        logger = logging.getLogger("test.plugin")

    plugin._set_context(FakeContext())  # type: ignore[arg-type]
    plugin._restart_auto_task()

    assert plugin._auto_task is None


def test_manifest_is_valid_json_and_matches_project_version() -> None:
    project_root = Path(__file__).resolve().parents[1]
    manifest = json.loads((project_root / "_manifest.json").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 2
    assert manifest["id"] == "local.maimai.bilibili-plugin"
    assert manifest["version"] == "0.6.0"
    assert manifest["host_application"]["max_version"] == "1.99.99"
    assert manifest["sdk"]["min_version"] == "2.8.0"
    assert {"llm.generate", "memory.ingest_summary", "send.text"} <= set(manifest["capabilities"])
    assert (project_root / "plugin.py").is_file()
    assert (project_root / "vendor" / "bilibili_mcp" / "mcp_server.py").is_file()


def test_mcp_server_entrypoint_is_intact() -> None:
    """防止误删 MCP 入口文件或把 stdio 启动行改坏。"""

    project_root = Path(__file__).resolve().parents[1]
    source = (project_root / "vendor" / "bilibili_mcp" / "mcp_server.py").read_text(encoding="utf-8")

    assert 'mcp.run(transport="stdio")' in source
    assert "bili_search" in source
    assert "bili_subtitle" in source
    assert "bili_comments" in source
    assert "bili_video_info" in source


def test_plugin_can_be_loaded_by_absolute_path_from_other_cwd(tmp_path: Path) -> None:
    """模拟 MaiBot Runner 按绝对路径加载插件，验证本地模块引导。"""

    project_root = Path(__file__).resolve().parents[1]
    code = f"""
import importlib.util
import json
import sys
from pathlib import Path

root = Path({json.dumps(str(project_root))})
sys.path = [item for item in sys.path if not item or Path(item).resolve() != root]
spec = importlib.util.spec_from_file_location("deployed_bilibili_plugin", root / "plugin.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
instance = module.create_plugin()
print(json.dumps(sorted(component["name"] for component in instance.get_components())))
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert json.loads(completed.stdout.strip()) == ["bili_recall", "bili_scroll"]


def test_background_task_can_be_scheduled() -> None:
    plugin = BilibiliPlugin()
    calls: list[tuple[str, str]] = []

    async def fake_run_scroll(keyword: str, stream_id: str) -> None:
        calls.append((keyword, stream_id))

    class FakeService:
        busy = False

    plugin._run_scroll_background = fake_run_scroll  # type: ignore[method-assign]
    plugin._scroll_service = FakeService()  # type: ignore[assignment]

    async def scenario() -> None:
        assert plugin._submit_background("MaiBot", "stream:test") is True
        await asyncio.gather(*plugin._background_tasks)

    asyncio.run(scenario())
    assert calls == [("MaiBot", "stream:test")]


@pytest.mark.asyncio
async def test_generate_recall_keeps_task_name_out_of_model_alias() -> None:
    plugin = BilibiliPlugin()
    calls: list[dict[str, object]] = []

    class FakeLLM:
        async def generate(self, prompt: str, **kwargs: object) -> dict[str, object]:
            del prompt
            calls.append(kwargs)
            return {"success": True, "response": "{}"}

    class FakeContext:
        llm = FakeLLM()

    plugin._set_context(FakeContext())  # type: ignore[arg-type]
    result = await plugin._generate_recall("test prompt", "utils", "", 0.4, 100)

    assert result["success"] is True
    assert calls[0]["model"] == ""
    assert calls[0]["task_name"] == "utils"
    assert calls[0]["model_name"] == ""


@pytest.mark.asyncio
async def test_load_persona_context_reads_host_config() -> None:
    plugin = BilibiliPlugin()
    plugin.set_plugin_config(plugin.get_default_config())
    values = {
        "bot.nickname": "猫猫",
        "personality.personality": "黑龙江高中生",
        "personality.behavior_style": "自然聊天",
        "personality.reply_style": "句尾加一个w",
    }

    class FakeConfig:
        async def get(self, key: str, default: object = None) -> object:
            return values.get(key, default)

    class FakeContext:
        logger = logging.getLogger("test.plugin")
        config = FakeConfig()

    plugin._set_context(FakeContext())  # type: ignore[arg-type]
    await plugin._load_persona_context()

    assert plugin._persona_context["nickname"] == "猫猫"
    assert plugin._persona_context["personality"] == "黑龙江高中生"
    assert plugin._persona_context["reply_style"] == "句尾加一个w"
