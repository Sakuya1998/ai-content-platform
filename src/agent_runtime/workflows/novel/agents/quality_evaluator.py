"""章节文学质量评审 Agent。"""

from typing import Any

from agent_runtime.execution.models import get_analyzer_llm
from agent_runtime.execution.structured import invoke_structured, parse_json_block
from agent_runtime.prompts import fill_template
from agent_runtime.workflows.novel.analysis.evaluation import JUDGE_RUBRIC_VERSION

JUDGE_DIMENSIONS = {
    "coherence",
    "character_consistency",
    "prose_style",
    "pacing",
    "scene_execution",
    "narrative_payoff",
}


class QualityEvaluatorAgent:
    """使用固定 rubric 输出可追踪的结构化文学评审。"""

    def __init__(self, llm=None):
        self.llm = llm or get_analyzer_llm()

    async def evaluate(
        self,
        *,
        novel: dict[str, Any],
        chapter: dict[str, Any],
        previous_chapters: list[dict[str, Any]],
        deterministic_report: dict[str, Any],
    ) -> dict[str, Any]:
        prompt = fill_template(
            "quality_evaluator",
            rubric_version=JUDGE_RUBRIC_VERSION,
            novel_title=novel.get("title", ""),
            genre=novel.get("genre", ""),
            style=novel.get("style", ""),
            inspiration=novel.get("inspiration", ""),
            previous_summaries="\n".join(
                f"- 第{item.get('chapter_number', '?')}章:{str(item.get('summary', ''))[:240]}"
                for item in previous_chapters[-5:]
            )
            or "暂无",
            scene_plan=repr(chapter.get("scene_plan") or []),
            deterministic_report=repr(deterministic_report),
            chapter_content=str(chapter.get("content", ""))[:12000],
        )

        def validate(items: list[dict[str, Any]]) -> None:
            if len(items) != 1:
                raise ValueError("必须只返回一个评测对象")
            scores = items[0].get("scores")
            if not isinstance(scores, dict) or set(scores) != JUDGE_DIMENSIONS:
                raise ValueError("scores 维度不完整")
            for name, value in scores.items():
                if not isinstance(value, (int, float)) or not 0 <= float(value) <= 100:
                    raise ValueError(f"{name} 必须是 0-100 分")
            findings = items[0].get("findings")
            if not isinstance(findings, list) or any(not isinstance(item, str) for item in findings):
                raise ValueError("findings 必须是字符串列表")

        _, items = await invoke_structured(
            self.llm,
            prompt,
            parser=parse_json_block,
            validator=validate,
            agent_name=type(self).__name__,
            format_name="JSON",
        )
        result = items[0]
        return {
            "rubric_version": JUDGE_RUBRIC_VERSION,
            "scores": {name: round(float(value), 1) for name, value in result["scores"].items()},
            "findings": [str(item)[:500] for item in result["findings"][:8]],
        }
