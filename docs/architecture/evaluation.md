# Evaluation 模块

Control 的 Evaluation 模块管理独立的评测策略版本、benchmark 报告和分析查询。Runtime 执行方式保持不变：Execution snapshot 冻结 CreationPolicy 中嵌入的 evaluation 配置，Runtime 使用该快照评测并通过完成事件返回报告。独立的 `evaluation_policies` 目前不自动绑定到 CreationPolicy，也不会覆盖运行中的冻结快照。

## 策略版本

`POST /api/v2/evaluations/policies` 创建 draft 版本。规则的 key、维度必须唯一，权重须为正数，阈值范围为 0 到 100。

```json
{
  "policy_id": "content-quality",
  "version": 1,
  "engine_version": "evaluation@1",
  "gate_threshold": 75,
  "critical_threshold": 60,
  "rules": [
    {
      "key": "text.length@1",
      "dimension": "length",
      "weight": 1,
      "critical": false,
      "parameters": {"scale": 100}
    }
  ]
}
```

`POST /api/v2/evaluations/policies/content-quality/v/1/publish` 发布版本。同一 workspace 和 policy ID 只保留一个 published 版本；发布新版本会在同一事务内归档旧版本。`POST /api/v2/evaluations/policies/content-quality/v/1/archive` 可归档已发布版本。所有策略读写按 workspace 隔离，变更会写入 audit log。

`GET /api/v2/evaluations/policies?policy_id=content-quality&status=published` 列出版本；`GET /api/v2/evaluations/policies/content-quality/v/1` 读取指定版本。

## Benchmark 与分析

原有 `POST /api/v2/evaluations/benchmarks` 仍提交异步 benchmark 操作，`GET /api/v2/evaluations/benchmarks` 和 `GET /api/v2/evaluations/benchmarks/{id}` 保持兼容。列表可按 `job_id` 和报告状态过滤。独立评测策略目前不注入 Runtime 快照，因此报告尚无可靠的独立 policy version 引用；数据库字段保留以便未来建立该关联。

```http
POST /api/v2/evaluations/benchmarks/{baseline_id}/compare
Content-Type: application/json

{"current_id":"run-2","threshold":3}
```

省略 `threshold` 时使用 3 分阈值。整体分数和双方共有的维度分数会返回 delta、improved/regressed/similar 状态；差值正好达到阈值时归类为改善或回退。

- `GET /api/v2/evaluations/stats?since=2026-10-01T00:00:00Z` 返回报告数量、均分、通过数和通过率。
- `GET /api/v2/evaluations/trends?dimension=length&limit=100` 返回该维度的时间点和趋势摘要。
- `GET /api/v2/evaluations/issues?limit=20` 按出现频次列出报告及 benchmark case 的 findings。
- `GET /api/v2/evaluations/dimensions?category=structure` 列出全局维度定义。

## 存储与兼容

Migration 023 为 `evaluation_benchmark_runs` 添加 job、policy version、overall score 和完成状态字段及查询索引；历史报告保留原 JSON，新字段保持 NULL 或使用 `completed` 默认值。新写入从 Runtime 报告中提取可查询元数据。查询仍能读取旧 JSON 报告。

`evaluation_policies` 使用 `app.workspace_id` RLS。内置和自定义维度存在全局共享的 `evaluation_dimensions` 表。benchmark 的写入仍由 Execution 完成事件事务通过 Evaluation writer 执行，避免与 job 状态投影分离提交。
