import pytest

from agent_runtime.workflows.novel.analysis.benchmark import BENCHMARK_CASES, run_evaluation_benchmark


@pytest.mark.asyncio
async def test_fixed_benchmark_is_deterministic_and_covers_required_categories():
    first = await run_evaluation_benchmark()
    second = await run_evaluation_benchmark()

    assert first["status"] == "passed"
    assert first["input_hash"] == second["input_hash"]
    assert [item["id"] for item in first["cases"]] == [item["id"] for item in BENCHMARK_CASES]
    assert {item["category"] for item in first["cases"]} == {
        "short_chapter_quality",
        "cross_chapter_consistency",
        "character_arc",
        "narrative_thread_payoff",
        "style_consistency",
    }


@pytest.mark.asyncio
async def test_benchmark_regression_gate_blocks_a_lower_candidate():
    baseline = await run_evaluation_benchmark()
    degraded = {**baseline, "cases": [dict(item) for item in baseline["cases"]]}
    degraded["cases"][0]["overall_score"] += 10

    candidate = await run_evaluation_benchmark(baseline=degraded)

    assert candidate["status"] == "failed"
    assert candidate["cases"][0]["regression_status"] == "regressed"
    assert candidate["cases"][0]["passed"] is False


@pytest.mark.asyncio
async def test_judge_failure_keeps_deterministic_scores():
    async def failing_judge(sample, deterministic):
        raise RuntimeError("provider unavailable")

    result = await run_evaluation_benchmark(include_judge=True, judge_case=failing_judge)

    assert result["status"] == "passed"
    assert result["judge_error"]
    assert all(item["deterministic_scores"] for item in result["cases"])
    assert all(item["judge_scores"] == {} for item in result["cases"])
