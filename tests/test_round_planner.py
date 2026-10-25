"""模型自主选题与选片的离线测试。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from round_planner import (
    build_planning_prompt,
    normalize_planned_keywords,
    plan_round,
    select_candidates,
)
from scroll_service import BilibiliScrollService, ScrollOptions, VideoCandidate

VIDEO_ONE = "BV1Plan000001"
VIDEO_TWO = "BV1Plan000002"


def make_candidates() -> list[VideoCandidate]:
    return [
        VideoCandidate(
            bvid=VIDEO_ONE,
            title="MaiBot 插件开发",
            author="UP1",
            play=1000,
            duration="10:00",
            description="",
            pubdate=1792850000,
            url=f"https://www.bilibili.com/video/{VIDEO_ONE}",
        ),
        VideoCandidate(
            bvid=VIDEO_TWO,
            title="AI Agent 实战",
            author="UP2",
            play=2000,
            duration="12:00",
            description="",
            pubdate=1792851000,
            url=f"https://www.bilibili.com/video/{VIDEO_TWO}",
        ),
    ]


class DummyService:
    def __init__(self, tmp_path: Path, llm_call: Any) -> None:
        self.options = ScrollOptions(
            keywords=["默认关键词"],
            model_directed=True,
            model_select_candidates=True,
            interest_profile="MaiBot 与 AI Agent",
            max_videos=2,
            planning_max_keywords=3,
        )
        self.llm_call = llm_call
        self.logger = logging.getLogger("test.round_planner")
        self.data_dir = tmp_path

    def _resolve_keywords(self, keyword: str) -> list[str]:
        return [keyword] if keyword else list(self.options.keywords)

    _extract_json_object = staticmethod(BilibiliScrollService._extract_json_object)  # noqa: SLF001
    _truncate = staticmethod(BilibiliScrollService._truncate)  # noqa: SLF001


def test_normalize_planned_keywords_rejects_too_generic_values() -> None:
    assert normalize_planned_keywords(["视频", "AI Agent", "AI Agent", "b站", "MaiBot 插件"]) == [
        "AI Agent",
        "MaiBot 插件",
    ]


@pytest.mark.asyncio
async def test_plan_round_uses_model_keywords(tmp_path: Path) -> None:
    calls: list[str] = []

    async def fake_llm(prompt: str, *args: object, **kwargs: object) -> dict[str, object]:
        del args, kwargs
        calls.append(prompt)
        return {
            "success": True,
            "response": json.dumps(
                {
                    "keywords": ["独立游戏开发", "AI 工作流"],
                    "search_order": "pubdate",
                    "reason": "探索相邻技术方向",
                },
                ensure_ascii=False,
            ),
        }

    service = DummyService(tmp_path, fake_llm)
    plan, error = await plan_round(service, "", {})

    assert error == ""
    assert plan["source"] == "model"
    assert plan["keywords"] == ["独立游戏开发", "AI 工作流"]
    assert calls


@pytest.mark.asyncio
async def test_select_candidates_only_accepts_returned_bvids(tmp_path: Path) -> None:
    async def fake_llm(prompt: str, *args: object, **kwargs: object) -> dict[str, object]:
        del prompt, args, kwargs
        return {
            "success": True,
            "response": json.dumps(
                {
                    "bvids": [VIDEO_TWO, "BV1Fake00000"],
                    "reason": "选择 AI Agent 实战",
                },
                ensure_ascii=False,
            ),
        }

    service = DummyService(tmp_path, fake_llm)
    selected, reason, error = await select_candidates(service, make_candidates(), {"keywords": ["AI"]})

    assert error == ""
    assert [item.bvid for item in selected] == [VIDEO_TWO]
    assert "AI Agent" in reason


def test_planning_prompt_includes_persona(tmp_path: Path) -> None:
    async def fake_llm(prompt: str, *args: object, **kwargs: object) -> dict[str, object]:
        del prompt, args, kwargs
        return {"success": True, "response": "{}"}

    service = DummyService(tmp_path, fake_llm)
    service.options.persona_context = {
        "nickname": "猫猫",
        "personality": "黑龙江高中生",
        "reply_style": "句尾加一个w",
    }
    prompt = build_planning_prompt(service, {})

    assert "猫猫" in prompt
    assert "黑龙江高中生" in prompt
    assert "句尾加一个w" in prompt
