"""模型自主选题与候选选片。

该模块只通过鸭子类型访问 ``BilibiliScrollService`` 的公共属性和少量工具方法，
避免把规划逻辑继续堆进主服务文件，也避免与 ``scroll_service`` 形成循环导入。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


async def plan_round(
    service: Any,
    keyword: str,
    seen: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """让模型根据兴趣画像和近期回想决定本轮搜索方向。"""

    fallback_keywords = service._resolve_keywords(keyword)
    fallback_plan: dict[str, Any] = {
        "keywords": fallback_keywords,
        "search_order": service.options.search_order,
        "reason": "使用配置关键词",
        "source": "fallback",
    }
    if keyword.strip() or not service.options.model_directed:
        service.logger.info(
            "跳过模型自主规划：%s",
            "使用显式关键词" if keyword.strip() else "model_directed=false",
        )
        return fallback_plan, ""

    service.logger.info("开始模型自主选题")
    prompt = build_planning_prompt(service, seen)
    try:
        result = await service.llm_call(
            prompt,
            service.options.model_task,
            service.options.model_name,
            0.3,
            1400,
        )
    except Exception as exc:
        service.logger.warning("模型规划异常，已回退默认关键词: %s: %s", type(exc).__name__, exc)
        return fallback_plan, f"模型规划失败，已回退默认关键词: {type(exc).__name__}: {exc}"
    if not isinstance(result, dict) or not bool(result.get("success", True)):
        reason = result.get("error") if isinstance(result, dict) else "返回格式异常"
        service.logger.warning("模型规划调用失败，已回退默认关键词: %s", reason)
        return fallback_plan, f"模型规划失败，已回退默认关键词: {reason}"

    response_text = extract_llm_text(result)
    payload = service._extract_json_object(response_text)
    keywords = normalize_planned_keywords(
        payload.get("keywords") if payload else None,
        max_keywords=service.options.planning_max_keywords,
    )
    if not keywords:
        service.logger.warning(
            "模型规划没有产生有效关键词，已回退默认关键词；response=%s",
            response_text[:800],
        )
        return fallback_plan, "模型规划没有返回有效关键词，已回退默认关键词"

    order = str(payload.get("search_order") or service.options.search_order).strip()
    if order not in {"pubdate", "totalrank", "click", "dm"}:
        order = service.options.search_order
    reason = service._truncate(str(payload.get("reason") or "").strip(), 300)
    plan = {
        "keywords": keywords,
        "search_order": order,
        "reason": reason,
        "source": "model",
    }
    service.logger.info("模型规划本轮B站浏览: keywords=%s order=%s reason=%s", keywords, order, reason)
    return plan, ""


def persona_prompt_text(service: Any, max_chars: int = 1600) -> str:
    """把插件读取到的 MaiBot 人设整理成提示片段。"""

    persona = getattr(service.options, "persona_context", None) or {}
    parts: list[str] = []
    nickname = str(persona.get("nickname") or "").strip()
    personality = str(persona.get("personality") or "").strip()
    reply_style = str(persona.get("reply_style") or "").strip()
    if nickname:
        parts.append(f"昵称：{nickname}")
    if personality:
        parts.append(f"身份与性格：{personality}")
    if reply_style:
        parts.append(f"说话风格：{reply_style}")
    text = "；".join(parts)
    return text[:max_chars]


def build_planning_prompt(service: Any, seen: dict[str, Any]) -> str:
    """构造自主选题 Prompt；只让模型决定搜索方向，不直接编造视频。"""

    recent_titles = load_recent_seen_titles(service, seen)
    recent_recalls = load_recent_recall_summaries(service, 5)
    seen_text = "\n".join(f"- {title}" for title in recent_titles) or "（暂无）"
    recall_text = "\n".join(f"- {summary}" for summary in recent_recalls) or "（暂无）"
    seed_keywords = "、".join(service._resolve_keywords("")) or "（无）"
    interest = service.options.interest_profile.strip() or "没有特别限定，选择有信息量、不过度重复的内容"
    persona = persona_prompt_text(service)
    now_text = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    max_keywords = max(1, int(service.options.planning_max_keywords))
    return (
        "你是 MaiBot 的B站内容策划。请根据长期兴趣、最近已经看过的视频和近期回想，"
        "自主决定本轮应该搜索什么视频。\n\n"
        f"当前时间：{now_text}\n"
        f"麦麦当前人设：{persona or '保持原有 MaiBot 人设，自然、有好奇心'}\n"
        f"长期兴趣画像：{interest}\n"
        f"默认关注种子：{seed_keywords}\n\n"
        "最近已经看过的视频标题：\n"
        f"{seen_text}\n\n"
        "最近几轮回想摘要：\n"
        f"{recall_text}\n\n"
        "输出要求：\n"
        "1. 不要输出思考过程、分析、解释或任何前后缀；你的整个回答必须从 { 开始、以 } 结束。\n"
        "2. 只输出一个 JSON 对象，不要 Markdown 代码围栏。\n"
        '3. 格式：{"keywords":["关键词1","关键词2"],"search_order":"pubdate",'
        '"reason":"一句话说明为什么选这些方向"}\n'
        f"4. 选择 1-{max_keywords} 个具体搜索关键词，每个 2-30 个字符；"
        "不要用“视频”“热门推荐”“B站”这类过宽词。\n"
        "5. 优先探索与兴趣画像相关、但近期没有重复看过的新方向；可以适度扩展到相邻话题。\n"
        "6. search_order 只能是 pubdate、totalrank、click、dm 之一。\n"
    )


async def select_candidates(
    service: Any,
    candidates: list[Any],
    plan: dict[str, Any],
) -> tuple[list[Any], str, str]:
    """让模型从搜索结果候选中挑出真正要读取的视频。"""

    prompt = build_candidate_selection_prompt(service, candidates, plan)
    try:
        result = await service.llm_call(
            prompt,
            service.options.model_task,
            service.options.model_name,
            0.4,
            800,
        )
    except Exception as exc:
        return [], "", f"模型选片失败，已回退按时间/播放排序: {type(exc).__name__}: {exc}"
    if not isinstance(result, dict) or not bool(result.get("success", True)):
        reason = result.get("error") if isinstance(result, dict) else "返回格式异常"
        return [], "", f"模型选片失败，已回退按时间/播放排序: {reason}"

    payload = service._extract_json_object(extract_llm_text(result))
    raw_bvids = payload.get("bvids") if payload else None
    if isinstance(raw_bvids, str):
        raw_bvids = [raw_bvids]
    allowed = {candidate.bvid: candidate for candidate in candidates}
    chosen: list[Any] = []
    for item in raw_bvids if isinstance(raw_bvids, list) else []:
        bvid = str(item or "").strip()
        if bvid in allowed and allowed[bvid] not in chosen:
            chosen.append(allowed[bvid])
        if len(chosen) >= max(1, service.options.max_videos):
            break
    reason = service._truncate(str(payload.get("reason") or "").strip(), 300) if payload else ""
    if not chosen:
        return [], reason, "模型选片没有返回有效 BV，已回退按时间/播放排序"
    service.logger.info("模型选择本轮视频: %s reason=%s", [item.bvid for item in chosen], reason)
    return chosen, reason, ""


def build_candidate_selection_prompt(
    service: Any,
    candidates: list[Any],
    plan: dict[str, Any],
) -> str:
    """构造候选视频筛选 Prompt。"""

    lines = [
        "你是 MaiBot 的B站选片助手。请从下面的搜索候选中，挑选最值得本轮阅读详情和字幕的视频。",
        f"麦麦当前人设：{persona_prompt_text(service) or '保持原有 MaiBot 人设'}",
        f"长期兴趣画像：{service.options.interest_profile.strip() or '无特别限定'}",
        f"本轮搜索方向：{'、'.join(str(x) for x in plan.get('keywords') or [])}",
        f"搜索理由：{plan.get('reason') or '无'}",
        f"本轮最多选择 {max(1, service.options.max_videos)} 条。",
        "",
        "候选列表：",
    ]
    for index, item in enumerate(candidates, start=1):
        pubdate = datetime.fromtimestamp(item.pubdate).strftime("%Y-%m-%d") if item.pubdate else "未知"
        lines.append(
            f"{index}. bvid={item.bvid} | 标题={item.title[:120]} | UP={item.author[:60]} | "
            f"播放={item.play} | 时长={item.duration or '未知'} | 发布={pubdate}"
        )
    lines.extend(
        [
            "",
            "输出要求：",
            '只输出 JSON：{"bvids":["BV..."],"reason":"一句话说明选择依据"}。',
            "只能选择候选列表中出现的 BV 号；不要编造，也不要选超过上限的数量。",
            "优先选择与兴趣画像相关、信息量高、近期且有字幕潜力的视频，避免重复和低信息量内容。",
        ]
    )
    return "\n".join(lines)


def load_recent_seen_titles(service: Any, seen: dict[str, Any], limit: int = 20) -> list[str]:
    """取出最近看过的标题，作为自主选题的去重上下文。"""

    entries = [value for value in seen.values() if isinstance(value, dict) and value.get("title")]
    entries.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
    return [service._truncate(str(item.get("title") or "").strip(), 80) for item in entries[:limit]]


def load_recent_recall_summaries(service: Any, limit: int = 5) -> list[str]:
    """读取最近几轮回想摘要，让模型知道最近刷过什么。"""

    recall_files: list[Path] = []
    for path in service.data_dir.glob("recalls/*/*.json"):
        if any(part.startswith(".") for part in path.parts):
            continue
        recall_files.append(path)
    recall_files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    summaries: list[str] = []
    for path in recall_files[: max(1, limit)]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        summary = str(payload.get("summary") or "").strip()
        nested = service._extract_json_object(summary)
        if nested and nested.get("summary"):
            summary = str(nested["summary"]).strip()
        if summary:
            summaries.append(service._truncate(summary, 260))
    return summaries


def normalize_planned_keywords(raw_keywords: Any, max_keywords: int = 3) -> list[str]:
    """清洗模型生成的关键词，拒绝空词和过宽泛词。"""

    if isinstance(raw_keywords, str):
        raw_values = re.split(r"[,，、;\n]+", raw_keywords)
    elif isinstance(raw_keywords, list):
        raw_values = raw_keywords
    else:
        raw_values = []
    banned = {"视频", "b站", "网站", "热门", "推荐", "随便", "看看", "内容"}
    normalized: list[str] = []
    for item in raw_values:
        value = str(item or "").strip().strip("\"'“”‘’")
        if not (2 <= len(value) <= 30):
            continue
        if value.lower() in banned:
            continue
        if value not in normalized:
            normalized.append(value)
    return normalized[: max(1, max_keywords)]


def extract_llm_text(result: dict[str, Any]) -> str:
    """统一提取 MaiBot LLM 响应中的文本字段。"""

    return str(
        result.get("response") or result.get("content") or result.get("text") or result.get("reasoning") or ""
    ).strip()
