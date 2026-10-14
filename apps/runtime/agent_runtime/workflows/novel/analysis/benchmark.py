"""固定小说质量评测样本、运行记录与回归门禁。"""

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_runtime.workflows.novel.analysis.evaluation import (
    DETERMINISTIC_SCHEMA_VERSION,
    JUDGE_RUBRIC_VERSION,
    combine_quality_scores,
    evaluate_chapter_deterministic,
)

BENCHMARK_SUITE_VERSION = "novel-quality-benchmark-v1"
JudgeCase = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]]]


def _sample_content(*paragraphs: str) -> str:
    return "\n\n".join(paragraphs)


BENCHMARK_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "short-chapter-quality",
        "category": "short_chapter_quality",
        "title": "短章结构与场景执行",
        "minimum_score": 78.0,
        "previous_chapters": [],
        "issues": [],
        "chapter": {
            "chapter_number": 1,
            "summary": "林寒在雾城门前识破假路引，并决定追查自己的旧身份。",
            "content": _sample_content(
                "夜雾压住城门，林寒在守卫伸手前收起旧路引，先问今日轮值的暗号。",
                "守卫答错了半句，身后的商队同时停步。林寒借着争执看见墙根刻着自己的旧剑记。",
                "追兵的马蹄声从长街逼近，他没有出城，反而转身走进雾最深的内城。",
            ),
            "scene_plan": [
                {
                    "scene_number": 1,
                    "goal": "通过城门",
                    "conflict": "守卫使用假暗号",
                    "turn": "发现旧剑记",
                    "estimated_words": 75,
                    "narrative_beats": [{"thread": "失忆身份", "action": "setup"}],
                },
                {
                    "scene_number": 2,
                    "goal": "摆脱追兵",
                    "conflict": "内外道路都被封锁",
                    "turn": "主动进入内城",
                    "estimated_words": 75,
                    "narrative_beats": [{"thread": "追兵来源", "action": "develop"}],
                },
            ],
            "scene_drafts": [
                {"scene_number": 1, "content": "城门试探与剑记发现。"},
                {"scene_number": 2, "content": "追兵逼近并转入内城。"},
            ],
        },
    },
    {
        "id": "cross-chapter-consistency",
        "category": "cross_chapter_consistency",
        "title": "跨章事实一致性",
        "minimum_score": 72.0,
        "previous_chapters": [
            {"chapter_number": 1, "summary": "林寒左臂受伤，随身佩剑折断。"},
        ],
        "issues": [
            {
                "severity": "medium",
                "description": "本章仍需解释上一章留下的左臂伤势。",
            }
        ],
        "chapter": {
            "chapter_number": 2,
            "summary": "林寒带伤追查折剑来源，并用残剑认出铸剑坊标记。",
            "content": _sample_content(
                "林寒用布带重新缠紧左臂，断剑只剩半尺，雨水沿缺口落在掌心。",
                "他避开正面交锋，借茶棚的铜镜盯住跟踪者，又从残剑纹路认出城西铸坊。",
                "天黑前，他把断剑藏进袖中，沿排水渠潜向那座已经封门十年的旧坊。",
            ),
            "scene_plan": [
                {
                    "scene_number": 1,
                    "goal": "追查断剑",
                    "conflict": "伤势限制行动",
                    "turn": "认出铸坊标记",
                    "estimated_words": 135,
                    "narrative_beats": [{"thread": "失忆身份", "action": "develop"}],
                }
            ],
            "scene_drafts": [{"scene_number": 1, "content": "带伤追踪并找到铸坊线索。"}],
        },
    },
    {
        "id": "character-arc",
        "category": "character_arc",
        "title": "角色动机与选择推进",
        "minimum_score": 78.0,
        "previous_chapters": [{"chapter_number": 2, "summary": "林寒仍习惯独自承担风险。"}],
        "issues": [],
        "chapter": {
            "chapter_number": 3,
            "summary": "林寒第一次把关键证据交给同伴，角色从戒备转向有限信任。",
            "content": _sample_content(
                "苏晚伸手要那枚铜印时，林寒本能地合拢五指，沉默比拒绝更清楚。",
                "巷口火把逼近，他终于把铜印放进她掌心，自己留下拖住搜捕者，并约定在钟楼会合。",
                "苏晚没有追问他的过去，只说会带着证据抵达。林寒第一次相信退路可以由另一个人守住。",
            ),
            "scene_plan": [
                {
                    "scene_number": 1,
                    "goal": "保护证据",
                    "conflict": "不信任同伴",
                    "turn": "主动交出铜印",
                    "estimated_words": 140,
                    "narrative_beats": [{"thread": "信任弧光", "action": "develop"}],
                }
            ],
            "scene_drafts": [{"scene_number": 1, "content": "交付证据并约定会合。"}],
        },
    },
    {
        "id": "narrative-thread-payoff",
        "category": "narrative_thread_payoff",
        "title": "叙事线程与伏笔回收",
        "minimum_score": 78.0,
        "previous_chapters": [{"chapter_number": 1, "summary": "城门留下三短一长的钟声伏笔。"}],
        "issues": [],
        "chapter": {
            "chapter_number": 4,
            "summary": "三短一长的钟声被证实是旧卫队撤离暗号，同时开启新的幕后人线索。",
            "content": _sample_content(
                "钟楼敲出三短一长，林寒终于想起那不是报时，而是旧卫队放弃城门时的撤离暗号。",
                "他按钟声间隔扳动四枚铜栓，尘封夹层滑开，里面只有一封写给现任城主的旧军令。",
                "伏笔有了答案，答案却把追查指向更高处。林寒收起军令，决定在庆典上当面验证城主。",
            ),
            "scene_plan": [
                {
                    "scene_number": 1,
                    "goal": "破解钟声",
                    "conflict": "钟楼机关即将锁死",
                    "turn": "暗号开启夹层",
                    "estimated_words": 140,
                    "narrative_beats": [
                        {"thread": "钟声暗号", "action": "resolve"},
                        {"thread": "城主真相", "action": "setup"},
                    ],
                }
            ],
            "scene_drafts": [{"scene_number": 1, "content": "破解暗号并发现旧军令。"}],
        },
    },
    {
        "id": "style-consistency",
        "category": "style_consistency",
        "title": "语言风格与重复控制",
        "minimum_score": 78.0,
        "previous_chapters": [],
        "issues": [],
        "chapter": {
            "chapter_number": 5,
            "summary": "用克制、短促的动作描写完成雨夜对峙。",
            "content": _sample_content(
                "雨落得直。刀光却斜。林寒退半步，让第一刀擦过衣襟。",
                "第二个人没有拔刀，只挡住巷口。林寒看了他一眼，忽然把断剑扔进积水。",
                "水花盖住机簧声。墙上的暗门弹开，他侧身没入门后，巷中只剩一截来不及落下的刀光。",
            ),
            "scene_plan": [
                {
                    "scene_number": 1,
                    "goal": "脱离包围",
                    "conflict": "前后夹击",
                    "turn": "断剑触发暗门",
                    "estimated_words": 115,
                    "narrative_beats": [{"thread": "断剑秘密", "action": "develop"}],
                }
            ],
            "scene_drafts": [{"scene_number": 1, "content": "以断剑机关脱身。"}],
        },
    },
)


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def benchmark_prompt_hash() -> str:
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "quality_evaluator.txt"
    try:
        content = prompt_path.read_bytes()
    except OSError:
        content = JUDGE_RUBRIC_VERSION.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def benchmark_input_hash() -> str:
    return _canonical_hash({"suite_version": BENCHMARK_SUITE_VERSION, "cases": BENCHMARK_CASES})


async def run_evaluation_benchmark(
    *,
    baseline: dict[str, Any] | None = None,
    include_judge: bool = False,
    judge_case: JudgeCase | None = None,
    model_provider: str = "",
    model_name: str = "",
    judge_setup_error: str = "",
    gate_threshold: float = 70.0,
    regression_threshold: float = 3.0,
) -> dict[str, Any]:
    """运行固定样本，并在模型评审失败时保留可用的确定性结果。"""
    normalized_gate = max(0.0, min(100.0, float(gate_threshold)))
    normalized_regression = abs(float(regression_threshold))
    baseline_cases = {
        str(item.get("id")): item for item in ((baseline or {}).get("cases") or []) if isinstance(item, dict)
    }
    cases: list[dict[str, Any]] = []
    run_errors: list[str] = [judge_setup_error] if judge_setup_error else []

    for sample in BENCHMARK_CASES:
        deterministic = evaluate_chapter_deterministic(
            sample["chapter"],
            issues=sample.get("issues") or [],
        )
        judge_scores: dict[str, float] = {}
        judge_findings: list[str] = []
        judge_error = judge_setup_error if include_judge and judge_case is None else ""
        if include_judge and judge_case is not None:
            try:
                judged = await judge_case(sample, deterministic)
                judge_scores = {
                    str(name): round(float(score), 1) for name, score in (judged.get("scores") or {}).items()
                }
                judge_findings = [str(item)[:500] for item in (judged.get("findings") or [])[:8]]
            except Exception as exc:
                judge_error = f"{type(exc).__name__}: {exc}"[:1000]
                run_errors.append(judge_error)

        overall = combine_quality_scores(deterministic["scores"], judge_scores)
        minimum = max(normalized_gate, float(sample.get("minimum_score", normalized_gate)))
        baseline_case = baseline_cases.get(str(sample["id"]))
        baseline_delta = None
        regression_status = "not_compared"
        if baseline_case is not None:
            baseline_delta = round(overall - float(baseline_case.get("overall_score", 0.0)), 1)
            regression_status = "regressed" if baseline_delta <= -normalized_regression else "stable"
        passed = overall >= minimum and regression_status != "regressed"
        cases.append(
            {
                "id": sample["id"],
                "category": sample["category"],
                "title": sample["title"],
                "input_hash": _canonical_hash(sample),
                "minimum_score": minimum,
                "deterministic_scores": deterministic["scores"],
                "judge_scores": judge_scores,
                "overall_score": overall,
                "findings": [
                    *deterministic["findings"],
                    *(
                        {"dimension": "judge", "score": None, "message": item, "source": "model_judge"}
                        for item in judge_findings
                    ),
                ],
                "judge_error": judge_error,
                "baseline_score": baseline_case.get("overall_score") if baseline_case else None,
                "baseline_delta": baseline_delta,
                "regression_status": regression_status,
                "passed": passed,
            }
        )

    overall_score = round(sum(item["overall_score"] for item in cases) / max(len(cases), 1), 1)
    passed = all(item["passed"] for item in cases)
    return {
        "suite_version": BENCHMARK_SUITE_VERSION,
        "evaluator_version": DETERMINISTIC_SCHEMA_VERSION,
        "rubric_version": JUDGE_RUBRIC_VERSION,
        "prompt_hash": benchmark_prompt_hash(),
        "input_hash": benchmark_input_hash(),
        "include_judge": include_judge,
        "model_provider": model_provider,
        "model_name": model_name,
        "baseline_run_id": (baseline or {}).get("id"),
        "gate_threshold": normalized_gate,
        "regression_threshold": normalized_regression,
        "overall_score": overall_score,
        "status": "passed" if passed else "failed",
        "judge_error": "; ".join(dict.fromkeys(item for item in run_errors if item))[:2000],
        "cases": cases,
    }
