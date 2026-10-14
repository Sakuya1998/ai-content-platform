"""共享状态定义(文档 4.1)。

LangGraph 的核心:所有节点通过 NovelState 读写共享数据,
StateGraph 在节点返回后自动做状态合并(chapters 按章节号追加或替换)。
"""

from typing import Annotated, Any, TypedDict

from agent_runtime.workflows.novel.memory.canon import empty_canon
from agent_runtime.workflows.novel.resources.brief import normalize_creative_brief
from agent_runtime.workflows.novel.resources.settings import GenerationSettings


def merge_chapters(
    existing: list[dict[str, Any]],
    updates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按章节号追加或替换终稿，支持完结后的非线性返修。"""
    merged = [dict(item) for item in existing]
    positions = {
        int(item.get("chapter_number", item.get("chapter", 0)) or 0): index for index, item in enumerate(merged)
    }
    for item in updates:
        chapter = dict(item)
        number = int(chapter.get("chapter_number", chapter.get("chapter", 0)) or 0)
        if number > 0 and number in positions:
            merged[positions[number]] = chapter
        else:
            if number > 0:
                positions[number] = len(merged)
            merged.append(chapter)
    return sorted(
        merged,
        key=lambda item: int(item.get("chapter_number", item.get("chapter", 0)) or 0),
    )


class NovelState(TypedDict, total=False):
    """小说创作的全局状态。"""

    # --- 用户输入 ---
    title: str  # 小说标题
    genre: str  # 类型:如"武侠"、"科幻"、"言情"
    inspiration: str  # 一句话灵感描述
    total_chapters: int  # 预计总章节数
    style: str  # 目标风格:对应 config.STYLE_PROFILES 的 key
    novel_id: str  # 小说标识(记忆/持久化用;空串表示内存运行)
    planning_review_enabled: bool  # 是否在设定与分镜阶段等待人工批准
    creative_brief: dict[str, Any]  # 目标读者、视角、主题和内容边界
    creative_brief_version: int  # 当前作品级创作约束版本
    creative_brief_review_required: bool  # 约束变更后当前待审稿是否必须重新质检
    content_type_snapshot: dict[str, Any]  # 创建时冻结的内容类型资源
    style_snapshot: dict[str, Any]  # 创建时冻结的风格资源
    creative_template_snapshot: dict[str, Any]  # 创建时冻结的创作模板
    quality_policy_snapshot: dict[str, Any]  # 创建时冻结的质量策略
    creation_policy_provenance: dict[str,str]

    # --- 创作状态 ---
    current_chapter: int  # 当前章节号
    current_phase: str  # 当前创作阶段
    chapter_plan: dict[str, Any]  # 当前章节的大纲(单章详情)
    scene_plan: list[dict[str, Any]]  # 当前章节的结构化场景执行计划
    current_draft: dict[str, Any]  # 当前章节草稿(写作/润色/质检间流转,定稿后并入 chapters)
    revision_notes: str  # 重写指引(一致性问题/人工反馈,供 SceneWriter 修正)
    revision_scene_number: int  # 0 表示整章修订;正整数表示仅重写指定场景
    candidate_source_hash: str  # 已选择候选稿的原始审查上下文，允许同批候选切换

    # --- 创作内容 ---
    world_bible: str  # 世界观圣经(YAML 文本)
    characters: list[dict[str, Any]]  # 角色列表(结构化)
    outline: list[dict[str, Any]]  # 章节大纲(结构化)
    chapters: Annotated[list[dict[str, Any]], merge_chapters]  # 已写章节，可按章替换
    canon: dict[str, Any]  # 结构化世界事实、角色状态和章节时间线
    replan_proposal: dict[str, Any]  # 最近一次定稿后的未来大纲重规划结果
    book_audit: dict[str, Any]  # 全书完结后的跨章质量审计
    book_audit_completed: bool  # 防止恢复或重放时重复执行终审
    memory_index: dict[str, Any]  # 章节、幕和全书三级长期记忆索引
    book_revision_mode: bool  # 是否正在返修一章已完成的终稿
    book_revision_origin_hash: str  # 发起返修时对应的全书终稿哈希

    # --- 质控与协作 ---
    issues: list[dict[str, Any]]  # 一致性问题
    quality_report: dict[str, Any]  # 自动质量门评分、结论与定向修订建议
    revision_count: int  # 当前章节修订次数
    max_revision_attempts: int  # 最大修订次数
    max_chapter_words: int  # 单章目标字数上限
    quality_gate_threshold: float  # 自动质量门综合分阈值
    human_feedback: str  # 人工反馈
    persistence_error: str  # 定稿持久化失败信息;非空时回到人工审查重试
    error: str  # 错误信息
    next_agent: str  # Orchestrator 决定的下一个 Agent


def create_initial_state(
    *,
    novel_id: str,
    title: str,
    genre: str,
    inspiration: str,
    total_chapters: int,
    style: str,
    planning_review_enabled: bool = False,
    creative_brief: dict[str, Any] | None = None,
    creative_brief_version: int = 1,
    content_type_snapshot: dict[str, Any] | None = None,
    style_snapshot: dict[str, Any] | None = None,
    creative_template_snapshot: dict[str, Any] | None = None,
    quality_policy_snapshot: dict[str, Any] | None = None,
    config: GenerationSettings | None = None,
) -> NovelState:
    """为所有入口构造一致的 LangGraph 初始状态。"""
    cfg = config or GenerationSettings()
    return {
        "title": title,
        "genre": genre,
        "inspiration": inspiration,
        "total_chapters": total_chapters,
        "style": style,
        "novel_id": novel_id,
        "planning_review_enabled": planning_review_enabled,
        "creative_brief": normalize_creative_brief(creative_brief),
        "creative_brief_version": max(int(creative_brief_version or 1), 1),
        "creative_brief_review_required": False,
        "content_type_snapshot": dict(content_type_snapshot or {}),
        "style_snapshot": dict(style_snapshot or {}),
        "creative_template_snapshot": dict(creative_template_snapshot or {}),
        "quality_policy_snapshot": dict(quality_policy_snapshot or {}),
        "current_chapter": 1,
        "current_phase": "writing",
        "max_revision_attempts": cfg.max_revision_attempts,
        "max_chapter_words": cfg.max_chapter_words,
        "quality_gate_threshold": cfg.quality_gate_threshold,
        "revision_count": 0,
        "revision_notes": "",
        "revision_scene_number": 0,
        "candidate_source_hash": "",
        "scene_plan": [],
        "chapters": [],
        "canon": empty_canon(),
        "book_audit": {},
        "book_audit_completed": False,
        "memory_index": {},
        "book_revision_mode": False,
        "book_revision_origin_hash": "",
    }
