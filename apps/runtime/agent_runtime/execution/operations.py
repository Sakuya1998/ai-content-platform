"""Project operations executed from durable MQ commands under a Runtime lease."""

import asyncio
from contextlib import contextmanager

from agent_runtime.execution.context import current_execution
from agent_runtime.execution.errors import JobCancelled
from agent_runtime.infrastructure.models.client import RuntimeModels
from agent_runtime.infrastructure.postgres.knowledge import KnowledgeOperations


class LeasedKnowledgeStore:
    def __init__(self, context):
        self.context = context

    @contextmanager
    def scope(self, workspace):
        context = self.context
        if workspace != context.job.command.workspace_id:
            raise ValueError("operation workspace conflict")
        with context.store.scope(workspace) as cursor:
            if context.store._owned(cursor, context.job, context.settings.worker_id) == "cancel_requested":
                raise JobCancelled()
            yield cursor


async def execute_operation(command, bundle):
    context = current_execution()
    request = bundle["request"]
    operation = request.get("runtime_operation") or command.payload.get("runtime_operation")
    arguments = request.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ValueError("operation arguments must be an object")
    workspace, project = command.workspace_id, command.project_id
    knowledge = KnowledgeOperations(LeasedKnowledgeStore(context))
    if operation == "memory.search":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 10000:
            raise ValueError("memory search query is required")
        models = RuntimeModels(context.settings)
        vector = (await models.embed([query]))[0]
        identity = models.embedding_identity()
        result = {
            "items": await asyncio.to_thread(
                knowledge.search,
                workspace,
                project,
                vector,
                model=identity["embedding_model"],
                index_version="memory@1",
                limit=arguments.get("limit", 5),
                embedding_identity=identity,
            )
        }
        for item in result["items"]:
            item.pop("embedding", None)
    elif operation == "memory.refresh":
        memories = await asyncio.to_thread(knowledge.list_memory, workspace, project)
        result = {
            "memory": {
                "items": [{key: value for key, value in item.items() if key != "embedding"} for item in memories]
            },
            "usage": await asyncio.to_thread(knowledge.usage, workspace, project),
            "traces": {"items": await asyncio.to_thread(knowledge.traces, workspace, project, 100)},
        }
    elif operation == "knowledge.publish":
        return await asyncio.to_thread(
            context.store.stage_knowledge_operation, context.job, context.settings.worker_id, operation, arguments
        )
    elif operation in {"knowledge.bind", "knowledge.unbind"}:
        pack_id = arguments.get("pack_id")
        if not isinstance(pack_id, str) or not pack_id:
            raise ValueError("knowledge pack ID is required")
        return await asyncio.to_thread(
            context.store.stage_knowledge_operation, context.job, context.settings.worker_id, operation, arguments
        )
    elif operation == "benchmark":
        from agent_runtime.workflows.novel.analysis.benchmark import run_evaluation_benchmark

        judge = None
        if arguments.get("include_judge"):
            from agent_runtime.workflows.novel.agents.quality_evaluator import QualityEvaluatorAgent

            agent = QualityEvaluatorAgent(llm=RuntimeModels(context.settings).chat("analysis"))

            async def judge(sample, deterministic):
                return await agent.evaluate(
                    novel={"total_chapters": 5},
                    chapter=sample["chapter"],
                    previous_chapters=sample.get("previous_chapters", []),
                    deterministic_report=deterministic,
                )

        result = await run_evaluation_benchmark(
            baseline=arguments.get("baseline"),
            include_judge=bool(arguments.get("include_judge")),
            judge_case=judge,
            gate_threshold=arguments.get("gate_threshold", 70),
            regression_threshold=arguments.get("regression_threshold", 3),
        )
    else:
        raise ValueError("unsupported Runtime operation")
    return {"operation": operation, "operation_result": result}
