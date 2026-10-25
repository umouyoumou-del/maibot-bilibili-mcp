"""麦麦刷B站插件。

通过 Firefox MCP（MCP Browser Kit）浏览B站搜索页/视频页，
读取详情、字幕和评论，再调用 MaiBot LLM 生成“B站回想”，
可选择写入 A_Memorix 长期记忆；也可切换回内置 Bilibili MCP。
"""

# ruff: noqa: E402

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Literal

# MaiBot 通过绝对路径 exec 插件入口，不保证插件目录在 sys.path 中。
# 必须在所有本地模块导入之前完成引导。
PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_DIR_TEXT = str(PLUGIN_DIR)
if PLUGIN_DIR_TEXT not in sys.path:
    sys.path.insert(0, PLUGIN_DIR_TEXT)

from maibot_sdk import CONFIG_RELOAD_SCOPE_SELF, Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from bilibili_mcp_client import BilibiliMCPClient
from firefox_bilibili_mcp import FirefoxBilibiliMCPClient
from scroll_service import BilibiliScrollService, ScrollOptions, ScrollRunResult

PLUGIN_VERSION = "0.6.0"
MCP_SERVER_PATH = PLUGIN_DIR / "vendor" / "bilibili_mcp" / "mcp_server.py"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default=PLUGIN_VERSION, description="插件配置版本")


class BilibiliConfig(PluginConfigBase):
    """B站 MCP 与请求配置。"""

    __ui_label__ = "B站"
    __ui_icon__ = "play-circle"
    __ui_order__ = 1

    credential_file: str = Field(
        default="bili_credential.json",
        description="可选的 B站凭证文件；相对路径以插件数据目录为基准",
    )
    request_timeout: float = Field(
        default=45.0,
        ge=5.0,
        le=180.0,
        description="单次 MCP 工具调用超时时间（秒）",
    )
    user_agent: str = Field(default=DEFAULT_USER_AGENT, description="访问 B站时使用的 User-Agent")


class FirefoxConfig(PluginConfigBase):
    """Firefox MCP（MCP Browser Kit）连接配置。"""

    __ui_label__ = "Firefox MCP"
    __ui_icon__ = "globe"
    __ui_order__ = 2

    mcp_url: str = Field(
        default="http://127.0.0.1:21082/mcp",
        description="常驻 Browser Kit HTTP MCP 地址；需由 MCPBrowserKit 启动脚本提供",
    )
    call_timeout_seconds: float = Field(
        default=120.0,
        ge=10.0,
        le=600.0,
        description="单次 Firefox MCP 工具调用超时（秒）",
    )
    page_load_timeout: float = Field(
        default=25.0,
        ge=5.0,
        le=120.0,
        description="等待 B站搜索页/视频页加载的超时（秒）",
    )


class ScrollConfig(PluginConfigBase):
    """刷B站与选片配置。"""

    __ui_label__ = "刷B站"
    __ui_icon__ = "scroll-text"
    __ui_order__ = 3

    data_source: Literal["firefox", "bilibili"] = Field(
        default="firefox",
        description="firefox=通过 Firefox MCP 浏览B站；bilibili=使用内置 Bilibili MCP API",
    )
    model_directed: bool = Field(
        default=True,
        description="让模型根据兴趣画像和近期回想自主决定每轮搜索关键词",
    )
    model_select_candidates: bool = Field(
        default=True,
        description="让模型从搜索结果中挑选真正要读取的视频",
    )
    interest_profile: str = Field(
        default="MaiBot、AI Agent、独立开发、开源工具、B站社区文化",
        description="模型自主选题时参考的长期兴趣画像",
    )
    use_persona: bool = Field(
        default=True,
        description="读取 MaiBot 的昵称、人格和说话风格，让模型按麦麦人设选题和写回想",
    )
    planning_max_keywords: int = Field(default=3, ge=1, le=5, description="模型每轮最多规划几个搜索关键词")
    candidate_pool_size: int = Field(default=24, ge=6, le=60, description="交给模型筛选的候选池大小")
    keywords: list[str] = Field(
        default_factory=lambda: ["MaiBot", "MaiMai", "AI Agent"],
        description="默认搜索关键词列表",
    )
    search_order: str = Field(default="pubdate", description="totalrank=综合 / click=播放 / pubdate=最新 / dm=弹幕")
    search_per_keyword: int = Field(default=6, ge=1, le=30, description="每个关键词搜索多少条候选")
    max_age_days: int = Field(default=7, ge=0, le=365, description="只刷最近多少天的视频；0 表示不限")
    max_videos: int = Field(default=6, ge=1, le=20, description="每轮最多读取多少条视频的详情")
    max_highlights: int = Field(default=3, ge=0, le=10, description="模型每轮最多选出多少条回想")
    include_comments: bool = Field(default=True, description="是否读取视频评论区作为模型材料")
    comments_per_video: int = Field(default=12, ge=0, le=100, description="每条视频最多读取多少条评论")
    comment_sort: Literal["hot", "new"] = Field(default="hot", description="评论区排序：hot=热度 / new=最新")
    include_comment_replies: bool = Field(default=True, description="是否读取评论的楼中楼回复")
    comment_digest_max_items: int = Field(
        default=4,
        ge=0,
        le=10,
        description="模型最多提炼多少条评论区观察",
    )
    subtitle_max_chars: int = Field(default=6000, ge=500, le=30000, description="每条字幕最多送入模型多少字符")
    total_material_chars: int = Field(default=24000, ge=4000, le=100000, description="本轮材料总字符上限")
    request_interval_seconds: float = Field(default=0.25, ge=0.0, le=10.0, description="MCP 工具调用间隔（秒）")
    auto_interval_minutes: int = Field(default=0, ge=0, le=10080, description="自动刷间隔；0 表示仅手动触发")
    target_stream_id: str = Field(default="", description="自动刷结果发送到的聊天流 ID；留空则只保存和写记忆")
    style_prompt: str = Field(
        default="用简洁、自然、像在分享刚刚刷到内容的口吻",
        description="回想文本的表达风格",
    )


class ModelConfig(PluginConfigBase):
    """回想生成模型配置。"""

    __ui_label__ = "模型与记忆"
    __ui_icon__ = "sparkles"
    __ui_order__ = 4

    task_name: str = Field(default="utils", description="MaiBot 模型任务名，例如 utils / planner / mid_memory")
    model_name: str = Field(default="", description="可选的指定模型名；留空使用任务名对应模型")
    temperature: float = Field(default=0.4, ge=0.0, le=2.0, description="采样温度")
    max_tokens: int = Field(default=1800, ge=200, le=8000, description="模型最大输出 token 数")
    ingest_memory: bool = Field(default=True, description="是否把回想写入 A_Memorix 长期记忆")
    memory_chat_id: str = Field(default="bilibili", description="自动刷时写入记忆使用的 chat_id")


class BilibiliPluginConfig(PluginConfigBase):
    """麦麦刷B站插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    bilibili: BilibiliConfig = Field(default_factory=BilibiliConfig)
    firefox: FirefoxConfig = Field(default_factory=FirefoxConfig)
    scroll: ScrollConfig = Field(default_factory=ScrollConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)


class BilibiliPlugin(MaiBotPlugin):
    """麦麦刷B站插件主类。"""

    config_model = BilibiliPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._scroll_service: BilibiliScrollService | None = None
        self._auto_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._persona_context: dict[str, str] = {}

    async def on_load(self) -> None:
        """插件加载后初始化 MCP 服务与自动刷任务。"""

        self._stop_event = asyncio.Event()
        await self._load_persona_context()
        self._scroll_service = self._build_scroll_service()
        self._restart_auto_task()
        self.ctx.logger.info("麦麦刷B站插件已加载，version=%s", PLUGIN_VERSION)

    async def on_unload(self) -> None:
        """插件卸载前停止后台任务。"""

        if self._stop_event is not None:
            self._stop_event.set()
        for task in list(self._background_tasks):
            task.cancel()
        if self._auto_task is not None:
            self._auto_task.cancel()
            self._auto_task = None
        self._scroll_service = None
        self.ctx.logger.info("麦麦刷B站插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置更新后重建服务与自动任务。"""

        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self.ctx.logger.info("麦麦刷B站插件配置已更新，version=%s", version)
            await self._load_persona_context()
            self._scroll_service = self._build_scroll_service()
            self._restart_auto_task()
        del config_data

    async def _load_persona_context(self) -> None:
        """从 MaiBot 全局配置读取麦麦人设，供选题和回想使用。"""

        self._persona_context = {}
        if not self.config.scroll.use_persona:
            self.ctx.logger.info("麦麦人设读取已关闭（scroll.use_persona=false）")
            return
        try:
            nickname = await self.ctx.config.get("bot.nickname", "麦麦")
            personality = await self.ctx.config.get("personality.personality", "")
            behavior_style = await self.ctx.config.get("personality.behavior_style", "")
            reply_style = await self.ctx.config.get("personality.reply_style", "")
        except Exception as exc:
            self.ctx.logger.warning("读取麦麦人设失败，将使用中性风格: %s", exc)
            return

        self._persona_context = {
            "nickname": str(nickname or "麦麦").strip(),
            "personality": str(personality or "").strip()[:1200],
            "behavior_style": str(behavior_style or "").strip()[:800],
            "reply_style": str(reply_style or "").strip()[:1200],
        }
        self.ctx.logger.info(
            "已读取麦麦人设：nickname=%s personality=%s字 reply_style=%s字",
            self._persona_context["nickname"],
            len(self._persona_context["personality"]),
            len(self._persona_context["reply_style"]),
        )

    def _build_scroll_service(self) -> BilibiliScrollService:
        """根据当前配置构造刷B站服务。"""

        data_dir = Path(self.ctx.paths.data_dir)
        runtime_dir = Path(self.ctx.paths.runtime_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        runtime_dir.mkdir(parents=True, exist_ok=True)

        credential_setting = str(self.config.bilibili.credential_file or "").strip()
        credential_file = Path(credential_setting) if credential_setting else data_dir / "bili_credential.json"
        if not credential_file.is_absolute():
            credential_file = data_dir / credential_file

        if self.config.scroll.data_source == "firefox":
            self.ctx.logger.info(
                "刷B站数据源：Firefox MCP（%s）；登录态使用 Firefox 当前B站账号",
                self.config.firefox.mcp_url,
            )
            mcp_client: Any = FirefoxBilibiliMCPClient(
                self.config.firefox.mcp_url,
                call_timeout_seconds=self.config.firefox.call_timeout_seconds,
                page_load_timeout=self.config.firefox.page_load_timeout,
                logger=self.ctx.logger,
            )
        else:
            self.ctx.logger.info(
                "刷B站数据源：Bilibili API MCP（可选凭证 %s）",
                credential_file,
            )
            mcp_client = BilibiliMCPClient(
                MCP_SERVER_PATH,
                credential_file=credential_file,
                tool_timeout_seconds=self.config.bilibili.request_timeout,
                log_path=runtime_dir / "bilibili_mcp.log",
                user_agent=self.config.bilibili.user_agent,
                logger=self.ctx.logger,
            )
        options = ScrollOptions(
            keywords=list(self.config.scroll.keywords),
            data_source=self.config.scroll.data_source,
            model_directed=self.config.scroll.model_directed,
            model_select_candidates=self.config.scroll.model_select_candidates,
            interest_profile=self.config.scroll.interest_profile,
            persona_context=dict(self._persona_context),
            planning_max_keywords=self.config.scroll.planning_max_keywords,
            candidate_pool_size=self.config.scroll.candidate_pool_size,
            search_order=self.config.scroll.search_order,
            search_per_keyword=self.config.scroll.search_per_keyword,
            max_age_days=self.config.scroll.max_age_days,
            max_videos=self.config.scroll.max_videos,
            max_highlights=self.config.scroll.max_highlights,
            include_comments=self.config.scroll.include_comments,
            comments_per_video=self.config.scroll.comments_per_video,
            comment_sort=self.config.scroll.comment_sort,
            include_comment_replies=self.config.scroll.include_comment_replies,
            comment_digest_max_items=self.config.scroll.comment_digest_max_items,
            subtitle_max_chars=self.config.scroll.subtitle_max_chars,
            total_material_chars=self.config.scroll.total_material_chars,
            model_task=self.config.model.task_name,
            model_name=self.config.model.model_name,
            temperature=self.config.model.temperature,
            max_tokens=self.config.model.max_tokens,
            ingest_memory=self.config.model.ingest_memory,
            memory_chat_id=self.config.model.memory_chat_id,
            request_interval_seconds=self.config.scroll.request_interval_seconds,
            style_prompt=self.config.scroll.style_prompt,
        )
        return BilibiliScrollService(
            data_dir=data_dir,
            options=options,
            mcp_client=mcp_client,
            llm_call=self._generate_recall,
            send_call=self._send_text,
            memory_ingest=self._ingest_memory,
            logger=self.ctx.logger,
        )

    def _restart_auto_task(self) -> None:
        """按配置重启定时刷任务。"""

        if self._auto_task is not None:
            self._auto_task.cancel()
            self._auto_task = None
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        interval = int(self.config.scroll.auto_interval_minutes)
        target_stream_id = str(self.config.scroll.target_stream_id or "").strip()
        if interval > 0 and self.config.plugin.enabled:
            self._auto_task = asyncio.create_task(self._auto_loop(interval), name="bilibili-auto-scroll")
            self.ctx.logger.info(
                "自动刷B站已启用：每 %s 分钟一轮；target_stream_id=%s",
                interval,
                target_stream_id or "<仅保存和写长期记忆>",
            )
        else:
            self.ctx.logger.info("自动刷B站未启用（auto_interval_minutes=%s）", interval)

    async def _auto_loop(self, interval_minutes: int) -> None:
        """按固定间隔自动跑一轮。"""

        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval_minutes * 60)
                return
            except TimeoutError:
                pass
            if self._stop_event.is_set():
                return
            service = self._scroll_service
            if service is None:
                continue
            stream_id = str(self.config.scroll.target_stream_id or "").strip()
            self.ctx.logger.info("自动刷B站定时任务触发")
            try:
                result = await service.run(keyword="", stream_id=stream_id)
                if result.success:
                    self.ctx.logger.info(
                        "自动刷B站完成：挑中 %s 条，保存=%s，记忆=%s",
                        len(result.bvids),
                        bool(result.saved_md),
                        bool(result.memory_result),
                    )
                else:
                    self.ctx.logger.warning("自动刷B站失败: %s", result.message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.ctx.logger.exception("自动刷B站异常")

    async def _generate_recall(
        self,
        prompt: str,
        task_name: str,
        model_name: str,
        temperature: float | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        """调用 MaiBot LLM 能力生成结构化回想。"""

        # 新版 Host 在显式传入 task_name 时，会把 model 当作具体模型名。
        # 因此这里 model 必须留空，避免把任务名 utils 误当成模型名。
        result = await self.ctx.llm.generate(
            prompt,
            model="",
            task_name=task_name or None,
            model_name=model_name,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if isinstance(result, dict):
            return result
        return {"success": False, "error": "LLM 能力返回格式异常", "model_name": model_name or task_name}

    async def _send_text(self, text: str, stream_id: str) -> Any:
        """通过 MaiBot 发送文本。"""

        return await self.ctx.send.text(text, stream_id)

    async def _ingest_memory(
        self,
        text: str,
        chat_id: str,
        external_id: str,
        tags: list[str],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """把生成好的B站回想写入 A_Memorix。"""

        try:
            result = await self.ctx.call_capability(
                "memory.ingest_summary",
                text=text,
                chat_id=chat_id,
                external_id=external_id,
                tags=tags,
                metadata=metadata,
                respect_filter=True,
            )
        except Exception as exc:
            return {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        if isinstance(result, dict):
            return result
        return {"success": bool(result), "result": result}

    async def _run_scroll_background(self, keyword: str, stream_id: str) -> None:
        """后台执行一轮刷B站，并汇报失败。"""

        service = self._scroll_service
        if service is None:
            await self._send_text("刷B站服务尚未初始化", stream_id)
            return
        try:
            result = await service.run(keyword=keyword, stream_id=stream_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.ctx.logger.exception("刷B站后台任务异常")
            await self._send_text(f"刷B站失败：{type(exc).__name__}: {exc}", stream_id)
            return

        if not result.success:
            await self._send_text(f"刷B站失败：{result.message}", stream_id)
            return
        # 没有新视频等提前返回的轮次不会走服务内的发送逻辑，这里补发。
        if result.markdown and not result.saved_md:
            await self._send_text(result.markdown, stream_id)

    def _submit_background(self, keyword: str, stream_id: str) -> bool:
        """提交一个后台刷B站任务，已有任务时返回 False。"""

        service = self._scroll_service
        if service is None or service.busy:
            return False
        task = asyncio.create_task(self._run_scroll_background(keyword, stream_id), name="bilibili-scroll")
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True

    @Command(
        "bili_scroll",
        description="通过 Firefox MCP 刷一轮B站并调用模型生成回想",
        pattern=r"^/bili_(?:scroll|refresh)(?:\s+(?P<keyword>.+))?$",
    )
    async def handle_scroll(
        self,
        stream_id: str = "",
        matched_groups: dict[str, Any] | None = None,
        **kwargs: object,
    ) -> tuple[bool, str, int]:
        """响应 /bili_scroll [关键词]。"""

        del kwargs
        if not self.config.plugin.enabled:
            return False, "麦麦刷B站插件已禁用", 0
        keyword = str((matched_groups or {}).get("keyword") or "").strip()
        if not self._submit_background(keyword, stream_id):
            return False, "上一轮刷B站还没有结束，请稍后再试", 0
        await self.ctx.send.text(
            f"开始刷B站{f'（关键词：{keyword}）' if keyword else ''}，正在通过 MCP 收集视频并生成回想……",
            stream_id,
        )
        return True, "已开始刷B站", 2

    @Tool(
        "bili_recall",
        description="通过 Firefox MCP 浏览B站、读取字幕/评论，并调用模型生成一份B站回想",
        parameters=[
            ToolParameterInfo(
                name="keyword",
                param_type=ToolParamType.STRING,
                description="可选搜索关键词；留空使用配置中的默认关键词",
                required=False,
            ),
        ],
    )
    async def handle_recall(self, keyword: str = "", **kwargs: object) -> dict[str, Any]:
        """同步执行一轮刷B站，把回想返回给调用方。"""

        del kwargs
        if not self.config.plugin.enabled:
            return {"success": False, "error": "麦麦刷B站插件已禁用"}
        service = self._scroll_service
        if service is None:
            return {"success": False, "error": "刷B站服务尚未初始化"}

        result: ScrollRunResult = await service.run(keyword=str(keyword or "").strip(), stream_id="")
        return {
            "success": result.success,
            "message": result.message,
            "markdown": result.markdown,
            "model_name": result.model_name,
            "bvids": result.bvids,
            "saved_json": result.saved_json,
            "saved_md": result.saved_md,
            "memory_result": result.memory_result,
            "selection_plan": result.selection_plan,
            "errors": result.errors,
        }


def create_plugin() -> BilibiliPlugin:
    """创建插件实例，供 MaiBot Runner 发现并加载。"""

    return BilibiliPlugin()
