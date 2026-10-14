"""结构化 Canon 构建、升级、人工治理与幂等更新测试。"""

import pytest

from agent_runtime.workflows.novel.memory.canon import (
    CANON_VERSION,
    apply_canon_operation,
    build_canon,
    empty_canon,
    ensure_canon,
    format_canon,
    record_final_chapter,
    replace_final_chapter,
)


def test_build_canon_extracts_world_characters_and_planned_timeline():
    canon = build_canon(
        world_bible="""```yaml
世界:
  城市: 雾都
  规则:
    - 夜晚禁止出城
```""",
        characters=[{"name": "林寒", "role": "主角", "personality": "谨慎"}],
        outline=[
            {
                "chapter": 1,
                "title": "雾起",
                "summary": "林寒入城",
                "time_days": 0,
                "emotion": "紧张",
                "characters": ["林寒"],
            }
        ],
    )

    assert canon["version"] == CANON_VERSION
    assert all(item["id"].startswith("world:") for item in canon["world_facts"])
    assert all(item["status"] == "active" for item in canon["world_facts"])
    assert {item["path"] for item in canon["world_facts"]} >= {"世界.城市", "世界.规则"}
    assert canon["characters"]["林寒"]["role"] == "主角"
    assert canon["timeline"][0]["status"] == "planned"
    assert canon["timeline"][0]["time_days"] == 0


def test_record_final_chapter_is_idempotent_and_updates_character_state():
    canon = build_canon(
        world_bible="城市: 雾都",
        characters=[{"name": "林寒", "role": "主角"}],
        outline=[{"chapter": 1, "title": "计划标题", "summary": "计划摘要"}],
    )
    chapter = {
        "chapter_number": 1,
        "title": "雾起",
        "summary": "林寒进入雾都",
        "characters": ["林寒"],
        "time_days": 0,
        "emotion": "紧张",
        "events": ["林寒发现城门封锁"],
        "locations": ["雾都城门"],
    }

    updated = record_final_chapter(canon, chapter)
    updated = record_final_chapter(updated, chapter)

    assert len(updated["timeline"]) == 1
    assert updated["timeline"][0]["status"] == "final"
    assert updated["characters"]["林寒"]["appearances"] == [1]
    assert updated["characters"]["林寒"]["last_seen_chapter"] == 1
    assert {item["id"] for item in updated["facts"]} == {
        "chapter:1:summary",
        "chapter:1:event:1",
        "chapter:1:location:1",
    }
    rendered = format_canon(updated, current_chapter=2)
    assert "状态=final" in rendered
    assert "林寒发现城门封锁" in rendered

    revised = record_final_chapter(
        updated,
        {
            **chapter,
            "summary": "林寒改道进入雾都",
            "events": ["林寒从暗门入城"],
            "locations": [],
        },
    )
    assert {item["id"] for item in revised["facts"]} == {
        "chapter:1:summary",
        "chapter:1:event:1",
    }
    assert not any(item["kind"] == "location" for item in revised["facts"])


def test_replace_final_chapter_removes_superseded_canon_effects():
    canon = empty_canon()
    canon["characters"] = {
        "旧角色": {"name": "旧角色", "appearances": [], "last_seen_chapter": 0},
        "新角色": {"name": "新角色", "appearances": [], "last_seen_chapter": 0},
    }
    canon["narrative_threads"] = [
        {
            "id": "thread:谜题",
            "title": "谜题",
            "managed_by": "outline",
            "status": "planned",
            "beats": [
                {
                    "id": "thread:谜题:beat:1:resolve:1",
                    "chapter": 1,
                    "action": "resolve",
                    "description": "揭晓谜底",
                    "status": "planned",
                }
            ],
        }
    ]
    original = record_final_chapter(
        canon,
        {
            "chapter_number": 1,
            "summary": "旧摘要",
            "characters": ["旧角色"],
            "narrative_beats": [
                {
                    "thread": "谜题",
                    "beat_id": "thread:谜题:beat:1:resolve:1",
                    "action": "resolve",
                    "description": "揭晓谜底",
                }
            ],
        },
    )

    replaced = replace_final_chapter(
        original,
        {
            "chapter_number": 1,
            "summary": "新摘要",
            "characters": ["新角色"],
            "narrative_beats": [],
        },
    )

    assert replaced["characters"]["旧角色"]["appearances"] == []
    assert replaced["characters"]["新角色"]["appearances"] == [1]
    assert [item["value"] for item in replaced["facts"]] == ["新摘要"]
    assert replaced["narrative_threads"][0]["status"] == "planned"
    assert replaced["narrative_threads"][0]["beats"][0]["status"] == "planned"


def test_narrative_threads_advance_from_planned_to_open_and_resolved():
    outline = [
        {
            "chapter": 1,
            "narrative_beats": [
                {
                    "thread": "失踪王印",
                    "action": "setup",
                    "description": "发现空印盒",
                    "priority": "major",
                    "due_chapter": 3,
                }
            ],
        },
        {
            "chapter": 2,
            "narrative_beats": [
                {
                    "thread": "失踪王印",
                    "action": "develop",
                    "description": "追踪伪造印文",
                    "priority": "major",
                    "due_chapter": 3,
                }
            ],
        },
        {
            "chapter": 3,
            "narrative_beats": [
                {
                    "thread": "失踪王印",
                    "action": "resolve",
                    "description": "揭示王印藏在剑鞘中",
                    "priority": "major",
                }
            ],
        },
    ]
    canon = build_canon(world_bible="", characters=[], outline=outline)
    thread = canon["narrative_threads"][0]

    assert thread["status"] == "planned"
    assert thread["priority"] == "major"
    assert thread["introduced_chapter"] == 1
    assert thread["due_chapter"] == 3
    assert [beat["action"] for beat in thread["beats"]] == ["setup", "develop", "resolve"]

    canon = record_final_chapter(canon, {"chapter_number": 1, **outline[0]})
    assert canon["narrative_threads"][0]["status"] == "open"
    assert canon["narrative_threads"][0]["beats"][0]["status"] == "completed"

    canon = record_final_chapter(canon, {"chapter_number": 3, **outline[2]})
    thread = canon["narrative_threads"][0]
    assert thread["status"] == "resolved"
    assert thread["resolved_chapter"] == 3


def test_legacy_foreshadowing_is_migrated_to_narrative_threads():
    canon = build_canon(
        world_bible="",
        characters=[],
        outline=[
            {"chapter": 1, "foreshadowing": ["埋设:旧剑来历"]},
            {"chapter": 4, "foreshadowing": ["回收:旧剑来历"]},
        ],
    )

    thread = canon["narrative_threads"][0]
    assert thread["title"] == "旧剑来历"
    assert thread["due_chapter"] == 4
    assert [beat["action"] for beat in thread["beats"]] == ["setup", "resolve"]


def test_ensure_canon_upgrades_missing_or_empty_checkpoint_state():
    kwargs = {
        "world_bible": "城市: 雾都",
        "characters": [{"name": "林寒"}],
        "outline": [{"chapter": 1, "title": "雾起"}],
        "chapters": [],
    }

    rebuilt_missing = ensure_canon(None, **kwargs)
    rebuilt_empty = ensure_canon(empty_canon(), **kwargs)

    assert rebuilt_missing["characters"]["林寒"]["name"] == "林寒"
    assert rebuilt_empty["timeline"][0]["chapter"] == 1
    assert "城市: 雾都" in format_canon(rebuilt_empty)


def test_ensure_canon_upgrades_v1_checkpoint_without_losing_content():
    legacy = {
        "version": 1,
        "world_facts": [{"path": "城市", "value": "雾都", "source": "world_builder"}],
        "characters": {"林寒": {"name": "林寒", "role": "主角"}},
        "timeline": [{"chapter": 1, "status": "final"}],
        "facts": [
            {
                "id": "chapter:1:summary",
                "kind": "chapter_summary",
                "subject": "第1章",
                "value": "林寒入城",
                "source": "chapter:1",
            }
        ],
    }

    upgraded = ensure_canon(
        legacy,
        world_bible="",
        characters=[],
        outline=[],
    )

    assert upgraded["version"] == CANON_VERSION
    assert upgraded["characters"]["林寒"]["role"] == "主角"
    assert upgraded["world_facts"][0]["id"].startswith("world:")
    assert upgraded["world_facts"][0]["status"] == "active"
    assert upgraded["facts"][0]["status"] == "active"
    assert upgraded["aliases"] == {}
    assert upgraded["audit"] == []


def test_human_fact_governance_tracks_audit_and_prompt_visibility():
    canon = build_canon(
        world_bible="城市: 雾都",
        characters=[],
        outline=[],
    )
    world_id = canon["world_facts"][0]["id"]

    updated = apply_canon_operation(
        canon,
        {
            "action": "upsert_fact",
            "target_type": "world_fact",
            "target_id": world_id,
            "path": "城市",
            "value": "新雾都",
            "reason": "统一新版地名",
        },
    )
    updated = apply_canon_operation(
        updated,
        {
            "action": "upsert_fact",
            "target_type": "fact",
            "subject": "议会",
            "kind": "organization",
            "value": "由七席组成",
            "reason": "补充剧情约束",
        },
    )
    manual_id = next(item["id"] for item in updated["facts"] if item["source"] == "manual")
    deprecated = apply_canon_operation(
        updated,
        {
            "action": "deprecate_fact",
            "target_type": "fact",
            "target_id": manual_id,
            "reason": "该设定已弃用",
        },
    )

    rendered = format_canon(deprecated)
    assert "新雾都" in rendered
    assert "由七席组成" not in rendered
    assert deprecated["facts"][0]["status"] == "deprecated"
    assert [item["action"] for item in deprecated["audit"]] == [
        "upsert_fact",
        "upsert_fact",
        "deprecate_fact",
    ]
    assert deprecated["audit"][-1]["reason"] == "该设定已弃用"
    assert deprecated["audit"][-1]["actor"] == "human"

    confirmed = apply_canon_operation(
        deprecated,
        {
            "action": "confirm_fact",
            "target_type": "fact",
            "target_id": manual_id,
            "reason": "重新确认有效",
        },
    )
    assert "由七席组成" in format_canon(confirmed)


def test_merge_alias_combines_character_history_and_resolves_future_appearances():
    canon = build_canon(
        world_bible="",
        characters=[
            {"name": "林寒", "role": "主角"},
            {"name": "寒鸦", "speech_pattern": "寡言"},
        ],
        outline=[],
    )
    canon = record_final_chapter(
        canon,
        {
            "chapter_number": 1,
            "summary": "寒鸦现身",
            "characters": ["寒鸦"],
        },
    )

    merged = apply_canon_operation(
        canon,
        {
            "action": "merge_alias",
            "alias": "寒鸦",
            "canonical_name": "林寒",
            "reason": "确认二者为同一角色",
        },
    )
    assert merged["aliases"] == {"寒鸦": "林寒"}
    assert "寒鸦" not in merged["characters"]
    assert merged["characters"]["林寒"]["appearances"] == [1]
    assert merged["characters"]["林寒"]["speech_pattern"] == "寡言"

    later = record_final_chapter(
        merged,
        {
            "chapter_number": 2,
            "summary": "寒鸦归来",
            "characters": ["寒鸦"],
        },
    )
    assert later["characters"]["林寒"]["appearances"] == [1, 2]
    assert "寒鸦 => 林寒" in format_canon(later)


def test_update_character_only_accepts_editable_fields():
    canon = build_canon(
        world_bible="",
        characters=[{"name": "林寒", "role": "主角"}],
        outline=[],
    )
    updated = apply_canon_operation(
        canon,
        {
            "action": "update_character",
            "name": "林寒",
            "patch": {"personality": "谨慎", "name": "不会生效"},
            "reason": "补充角色性格",
        },
    )

    assert updated["characters"]["林寒"]["name"] == "林寒"
    assert updated["characters"]["林寒"]["personality"] == "谨慎"
    assert updated["audit"][-1]["target"] == "character:林寒"


def test_human_can_govern_narrative_thread_and_beats():
    canon = apply_canon_operation(
        empty_canon(),
        {
            "action": "upsert_thread",
            "title": "失踪王印",
            "description": "王印去向之谜",
            "kind": "mystery",
            "priority": "major",
            "introduced_chapter": 1,
            "due_chapter": 5,
            "reason": "新增主线剧情债务",
        },
    )
    thread_id = canon["narrative_threads"][0]["id"]
    canon = apply_canon_operation(
        canon,
        {
            "action": "upsert_thread_beat",
            "target_id": thread_id,
            "chapter": 5,
            "beat_action": "resolve",
            "description": "揭示王印藏处",
            "reason": "确定回收节点",
        },
    )
    canon = apply_canon_operation(
        canon,
        {
            "action": "update_thread_status",
            "target_id": thread_id,
            "status": "resolved",
            "resolved_chapter": 5,
            "reason": "人工确认已经回收",
        },
    )

    thread = canon["narrative_threads"][0]
    assert thread["status"] == "resolved"
    assert thread["beats"][0]["action"] == "resolve"
    assert [entry["action"] for entry in canon["audit"]] == [
        "upsert_thread",
        "upsert_thread_beat",
        "update_thread_status",
    ]


@pytest.mark.parametrize(
    "operation, message",
    [
        ({"action": "upsert_fact", "value": "内容"}, "原因"),
        (
            {
                "action": "deprecate_fact",
                "target_type": "unknown",
                "target_id": "missing",
                "reason": "测试",
            },
            "target_type",
        ),
        (
            {
                "action": "confirm_fact",
                "target_id": "missing",
                "reason": "测试",
            },
            "不存在",
        ),
        (
            {
                "action": "merge_alias",
                "alias": "寒鸦",
                "canonical_name": "林寒",
                "reason": "测试",
            },
            "规范角色不存在",
        ),
        (
            {
                "action": "update_character",
                "name": "林寒",
                "patch": {"unknown": "value"},
                "reason": "测试",
            },
            "角色不存在",
        ),
    ],
)
def test_invalid_human_canon_operations_raise(operation, message):
    with pytest.raises(ValueError, match=message):
        apply_canon_operation(empty_canon(), operation)
