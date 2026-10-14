"""分析工具:时间线校验、角色行为验证、节奏分析(文档 6.1)。

确定性纯函数实现,不依赖 LLM —— 把可计算的检查交给代码,
LLM 只处理需要语义理解的部分。
"""

from collections import Counter
from typing import Any

from langchain_core.tools import tool

from agent_runtime.workflows.novel.memory.canon import narrative_beats_from_chapter
from agent_runtime.workflows.novel.resources.brief import normalize_creative_brief


def _timeline_diagnostics(chapters: list[dict]) -> tuple[list[tuple[int, int | None]], list[str]]:
    timeline: list[tuple[int, int | None]] = []
    normalized = [(int(ch.get("chapter", ch.get("chapter_number", 0)) or 0), ch) for ch in chapters]
    for number, ch in sorted(normalized, key=lambda item: item[0]):
        raw_days = ch.get("time_days")
        days = int(raw_days) if raw_days is not None else None
        timeline.append((number, days))

    conflicts: list[str] = []
    prev_num, prev_days = -1, None
    for num, days in timeline:
        if days is None:
            continue
        if prev_days is not None and days < prev_days:
            conflicts.append(f"第{num}章(time_days={days})早于第{prev_num}章(time_days={prev_days}),时间线倒流")
        prev_num, prev_days = num, days
    return timeline, conflicts


@tool
def calculate_timeline(chapters: list[dict]) -> str:
    """检查章节时间线一致性,输出每章时间标记与检测到的冲突。

    Args:
        chapters: 章节列表,每章需含 chapter/summary,可含 time_days(距开篇天数)

    Returns:
        时间线清单与冲突报告
    """
    timeline, conflicts = _timeline_diagnostics(chapters)

    lines = [f"时间线:{[(n, d) for n, d in timeline]}"]
    lines.append("时间线冲突:" + ("; ".join(conflicts) if conflicts else "无"))
    return "\n".join(lines)


def _character_behavior_diagnostic(
    character_name: str,
    action: str,
    character_setting: str,
) -> tuple[bool, list[str]]:
    setting_lows = character_setting.lower()
    action_words = [w for w in action.replace(",", " ").split() if w]
    misses = [w for w in action_words if w.lower() not in setting_lows]
    return not misses, misses


@tool
def check_character_behavior(
    character_name: str,
    action: str,
    character_setting: str,
) -> str:
    """验证角色行为是否符合其人设设定。

    Args:
        character_name: 角色名
        action: 待验证的行为描述(一句话)
        character_setting: 角色设定文本(性格/行为模式/口头禅)

    Returns:
        一致/存疑 的启发式判定与依据(最终语义裁决交给 ConsistencyChecker)
    """
    # 启发式:行为关键词未在设定中出现即标记存疑,交 LLM 复核
    passed, misses = _character_behavior_diagnostic(character_name, action, character_setting)
    if passed:
        return f"「{character_name}」行为与设定关键词一致(启发式通过,建议 LLM 复核语义)。"
    return f"「{character_name}」行为中的词 {misses} 未在设定中出现,存在人设偏离风险,需一致性检查 Agent 复核。"


@tool
def analyze_pacing(chapters: list[dict]) -> str:
    """分析小说节奏分布,统计各情绪基调占比与连续同调警示。

    Args:
        chapters: 章节列表,每章需含 emotion(情绪基调)

    Returns:
        节奏分布报告
    """
    if not chapters:
        return "暂无章节可分析。"

    counts, warnings = _pacing_diagnostics(chapters)
    total = sum(counts.values())
    dist = ", ".join(f"{k} {v}章({v * 100 // total}%)" for k, v in counts.items())

    report = [f"节奏分布(共{total}章):{dist}"]
    report.append("节奏警示:" + ("; ".join(warnings) if warnings else "无"))
    return "\n".join(report)


def _pacing_diagnostics(chapters: list[dict]) -> tuple[dict[str, int], list[str]]:
    emotions = [str(c.get("emotion", "未知")).strip() or "未知" for c in chapters]
    counts: dict[str, int] = {}
    for emotion in emotions:
        counts[emotion] = counts.get(emotion, 0) + 1

    warnings: list[str] = []
    if emotions:
        streak = 1
        for index in range(1, len(emotions)):
            if emotions[index] == emotions[index - 1]:
                streak += 1
                if streak == 3:
                    warnings.append(f"「{emotions[index]}」连续 {streak} 章及以上,节奏趋于单调")
            else:
                streak = 1
    return counts, warnings


def _narrative_key(value: dict[str, Any]) -> tuple[str, str]:
    return (
        " ".join(str(value.get("thread", value.get("title", ""))).casefold().split()),
        str(value.get("action", "")).casefold(),
    )


def build_narrative_thread_diagnostics(
    *,
    canon: dict[str, Any] | None,
    chapter: dict[str, Any],
    total_chapters: int | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """确定性检查叙事线程截止期、最终回收和场景分配。"""
    number = int(chapter.get("chapter_number", chapter.get("chapter", 0)) or 0)
    threads = (canon or {}).get("narrative_threads") or []
    issues: list[dict[str, Any]] = []
    reports: list[str] = []
    chapter_beats = Counter(_narrative_key(beat) for beat in narrative_beats_from_chapter(chapter))
    scene_beats = Counter(
        _narrative_key(beat)
        for scene in (chapter.get("scene_plan") or [])
        for beat in (scene.get("narrative_beats") or [])
        if isinstance(beat, dict)
    )
    expected_beats: Counter[tuple[str, str]] = Counter()

    for thread in threads:
        status = str(thread.get("status", "planned"))
        if status in {"resolved", "abandoned"}:
            continue
        title = str(thread.get("title", "未命名线程"))
        priority = str(thread.get("priority", "minor"))
        due = int(thread.get("due_chapter", 0) or 0)
        resolving_here = chapter_beats[(" ".join(title.casefold().split()), "resolve")] > 0
        for beat in thread.get("beats") or []:
            if int(beat.get("chapter", 0) or 0) == number:
                expected_beats[_narrative_key({"thread": title, **beat})] += 1

        if due and due < number and not resolving_here:
            issues.append(
                {
                    "type": "narrative_thread_overdue",
                    "description": f"叙事线程「{title}」计划在第{due}章前回收,当前仍为 {status}",
                    "chapter": number,
                    "severity": "high" if priority == "major" else "medium",
                    "suggestion": "在本章安排明确回收,或由人工调整截止章/标记放弃",
                    "source": "deterministic",
                    "thread_id": thread.get("id"),
                }
            )
        if total_chapters and number >= total_chapters and priority == "major" and not resolving_here:
            issues.append(
                {
                    "type": "narrative_thread_unresolved",
                    "description": f"最终章结束前主要叙事线程「{title}」仍未解决",
                    "chapter": number,
                    "severity": "high",
                    "suggestion": "在最终章完成明确回收,或由人工裁决为 abandoned",
                    "source": "deterministic",
                    "thread_id": thread.get("id"),
                }
            )

    missing_from_chapter = expected_beats - chapter_beats
    for (title, action), count in missing_from_chapter.items():
        issues.append(
            {
                "type": "narrative_beat_missing",
                "description": f"第{number}章缺少计划叙事 beat:{title}/{action} ({count} 次)",
                "chapter": number,
                "severity": "high" if action == "resolve" else "medium",
                "suggestion": "按大纲补回该叙事 beat,并在场景计划中明确承载场景",
                "source": "deterministic",
            }
        )

    if chapter_beats != scene_beats:
        issues.append(
            {
                "type": "narrative_beat_scene_coverage",
                "description": "本章 narrative beat 与场景计划中的分配不一致",
                "chapter": number,
                "severity": "medium",
                "suggestion": "确保每个章节叙事 beat 恰好分配到一个场景",
                "source": "deterministic",
            }
        )

    open_summary = [
        f"{item.get('title')}[{item.get('priority', 'minor')}/{item.get('status', 'planned')}]"
        f" due={item.get('due_chapter') or '未定'}"
        for item in threads
        if item.get("status") not in {"resolved", "abandoned"}
    ]
    reports.append("开放叙事线程:" + ("; ".join(open_summary) if open_summary else "无"))
    reports.append(f"本章计划 beat:{list(expected_beats.elements())}")
    reports.append(f"本章元数据 beat:{list(chapter_beats.elements())}")
    reports.append(f"场景分配 beat:{list(scene_beats.elements())}")
    return issues, "\n".join(reports)


def build_consistency_diagnostics(
    *,
    chapter: dict[str, Any],
    characters: list[dict[str, Any]],
    outline: list[dict[str, Any]],
    previous_chapters: list[dict[str, Any]],
    max_chapter_words: int | None = None,
    canon: dict[str, Any] | None = None,
    total_chapters: int | None = None,
    creative_brief: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """运行无需 LLM 的检查,并返回结构化问题与可注入 Prompt 的报告。"""
    number = int(chapter.get("chapter_number", chapter.get("chapter", 0)) or 0)
    content = str(chapter.get("content", "")).strip()
    issues: list[dict[str, Any]] = []
    reports: list[str] = []

    if not content:
        issues.append(
            {
                "type": "chapter_structure",
                "description": "章节正文为空",
                "chapter": number,
                "severity": "high",
                "suggestion": "重新生成完整章节正文",
                "source": "deterministic",
            }
        )
    if max_chapter_words and len(content) > max_chapter_words:
        issues.append(
            {
                "type": "chapter_length",
                "description": f"章节正文长度 {len(content)} 超过目标上限 {max_chapter_words}",
                "chapter": number,
                "severity": "high",
                "suggestion": f"压缩正文至不超过 {max_chapter_words} 个字符,保留关键情节",
                "source": "deterministic",
            }
        )

    timeline_input = [*previous_chapters, chapter]
    timeline, conflicts = _timeline_diagnostics(timeline_input)
    reports.append(f"时间线:{timeline}")
    reports.append("时间线冲突:" + ("; ".join(conflicts) if conflicts else "无"))
    for conflict in conflicts:
        issues.append(
            {
                "type": "timeline",
                "description": conflict,
                "chapter": number,
                "severity": "high",
                "suggestion": "修正本章时间标记或与前章的事件顺序",
                "source": "deterministic",
            }
        )

    pacing_input = [
        entry for entry in (outline or [*previous_chapters, chapter]) if str(entry.get("emotion", "")).strip()
    ]
    counts, warnings = _pacing_diagnostics(pacing_input)
    reports.append(f"节奏分布:{counts}")
    reports.append("节奏警示:" + ("; ".join(warnings) if warnings else "无"))
    for warning in warnings:
        issues.append(
            {
                "type": "pacing",
                "description": warning,
                "chapter": number,
                "severity": "medium",
                "suggestion": "调整本章情绪基调或场景节奏,避免连续同调",
                "source": "deterministic",
            }
        )

    summary = str(chapter.get("summary", "")).strip()
    if summary and characters:
        behavior_reports = []
        for character in characters:
            name = str(character.get("name", "未知角色"))
            setting = repr(character)
            passed, misses = _character_behavior_diagnostic(name, summary, setting)
            behavior_reports.append(f"{name}: {'启发式通过' if passed else f'存疑,未匹配词={misses}'}")
        reports.append("角色行为启发式:" + "; ".join(behavior_reports))

    narrative_issues, narrative_report = build_narrative_thread_diagnostics(
        canon=canon,
        chapter=chapter,
        total_chapters=total_chapters,
    )
    issues.extend(narrative_issues)
    reports.append(narrative_report)

    brief = normalize_creative_brief(creative_brief)
    lowered_content = content.casefold()
    avoided_hits = [phrase for phrase in brief["avoid_content"] if phrase.casefold() in lowered_content]
    reports.append("创作约束回避项命中:" + ("；".join(avoided_hits) if avoided_hits else "无"))
    for phrase in avoided_hits[:8]:
        issues.append(
            {
                "type": "creative_brief_boundary",
                "description": f"正文命中了创作约束中的回避内容：{phrase}",
                "chapter": number,
                "severity": "high",
                "suggestion": f"删除或改写与“{phrase}”相关的内容，并保持原有情节功能",
                "source": "deterministic",
            }
        )

    if total_chapters and number >= total_chapters and brief["must_include"]:
        manuscript = "\n".join(str(item.get("content", "")) for item in [*previous_chapters, chapter]).casefold()
        missing = [phrase for phrase in brief["must_include"] if phrase.casefold() not in manuscript]
        reports.append("全书必须包含项缺失:" + ("；".join(missing) if missing else "无"))
        for phrase in missing[:8]:
            issues.append(
                {
                    "type": "creative_brief_requirement",
                    "description": f"全书终章仍未兑现必须包含项：{phrase}",
                    "chapter": number,
                    "severity": "high",
                    "suggestion": f"在不破坏既有因果的前提下兑现“{phrase}”",
                    "source": "deterministic",
                }
            )

    return issues, "\n".join(reports)
