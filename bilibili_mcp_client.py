"""MaiBot 插件侧的最小 Bilibili MCP 客户端。

通过 MCP stdio transport 拉起 ``vendor/bilibili_mcp/mcp_server.py``，
把工具返回的 JSON 文本还原成 Python 字典。客户端每轮创建一次连接，
避免长期占用子进程和 B站连接。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class BilibiliMCPError(RuntimeError):
    """Bilibili MCP 调用失败。"""


class BilibiliMCPConnection:
    """一轮刷B站期间复用的 MCP 会话。"""

    def __init__(self, session: ClientSession, tool_timeout_seconds: float) -> None:
        self._session = session
        self._tool_timeout_seconds = tool_timeout_seconds

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用一个 MCP 工具，并把 JSON 结果解析成字典。"""

        timeout = timedelta(seconds=max(1.0, self._tool_timeout_seconds))
        try:
            result = await self._session.call_tool(name, arguments or {}, read_timeout_seconds=timeout)
        except Exception as exc:
            raise BilibiliMCPError(f"MCP 工具 {name} 调用失败: {type(exc).__name__}: {exc}") from exc

        text_parts = [
            str(getattr(item, "text", "") or "")
            for item in (getattr(result, "content", None) or [])
            if getattr(item, "text", None) is not None
        ]
        text = "\n".join(part for part in text_parts if part.strip()).strip()
        if bool(getattr(result, "isError", False)):
            raise BilibiliMCPError(text or f"MCP 工具 {name} 返回错误")

        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
        if isinstance(payload, dict):
            return payload
        return {"result": payload}


class BilibiliMCPClient:
    """按需拉起只读 Bilibili MCP Server 的 stdio 客户端。"""

    def __init__(
        self,
        server_path: Path,
        *,
        python_executable: str | None = None,
        credential_file: Path | None = None,
        tool_timeout_seconds: float = 45.0,
        log_path: Path | None = None,
        user_agent: str = "",
        logger: logging.Logger | None = None,
    ) -> None:
        self.server_path = Path(server_path).resolve()
        self.python_executable = python_executable or sys.executable
        self.credential_file = Path(credential_file).resolve() if credential_file else None
        self.tool_timeout_seconds = max(1.0, float(tool_timeout_seconds))
        self.log_path = Path(log_path).resolve() if log_path else None
        self.user_agent = str(user_agent or "").strip()
        self.logger = logger or logging.getLogger("maibot.bilibili.mcp")

    def _build_environment(self) -> dict[str, str]:
        """构造 MCP 子进程环境，并强制 UTF-8 避免 Windows 管道乱码。"""

        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["BILI_REQUEST_TIMEOUT"] = str(self.tool_timeout_seconds)
        if self.credential_file is not None:
            environment["BILI_CREDENTIAL_FILE"] = str(self.credential_file)
        if self.user_agent:
            environment["BILI_USER_AGENT"] = self.user_agent
        return environment

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[BilibiliMCPConnection]:
        """启动 MCP Server 并返回可用连接。"""

        if not self.server_path.is_file():
            raise BilibiliMCPError(f"Bilibili MCP Server 不存在: {self.server_path}")

        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            error_stream = self.log_path.open("a", encoding="utf-8", errors="replace")
        else:
            error_stream = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

        parameters = StdioServerParameters(
            command=self.python_executable,
            args=[str(self.server_path)],
            env=self._build_environment(),
            cwd=str(self.server_path.parent),
            encoding="utf-8",
            encoding_error_handler="replace",
        )

        try:
            self.logger.info("启动 Bilibili MCP Server: %s", self.server_path)
            with error_stream:
                async with stdio_client(parameters, errlog=error_stream) as (read_stream, write_stream):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=self.tool_timeout_seconds),
                    ) as session:
                        await session.initialize()
                        self.logger.info("Bilibili MCP Server 已就绪")
                        yield BilibiliMCPConnection(session, self.tool_timeout_seconds)
        except BilibiliMCPError:
            raise
        except Exception as exc:
            raise BilibiliMCPError(f"Bilibili MCP Server 启动失败: {type(exc).__name__}: {exc}") from exc
