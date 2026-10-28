"""Restore historical jobs, events, usage and audit without restarting old work."""

from uuid import NAMESPACE_URL, uuid5

from agent_runtime.domain.protocol import RuntimeCommand, RuntimeEvent
from psycopg.types.json import Jsonb

from tools.migrate.platform_legacy_export import stable_id


def import_history(control, runtime, tables, decode, workspace, project, old_id, import_job, source_hash):
    jobs = {}
    for row in tables.get("run_jobs", []):
        if row.get("novel_id") != old_id:
            continue
        identity = stable_id("job", row["id"])
        jobs[row["id"]] = identity
        original = row.get("status", "failed")
        status = original if original in {"completed", "failed", "cancelled", "waiting_review"} else "failed"
        error = (
            {"type": "LegacyInterrupted", "original_status": original}
            if status != original
            else {"message": row.get("error") or ""}
        )
        payload = {
            "legacy_job_id": row["id"],
            "legacy_action": row.get("action"),
            "request": decode(row).get("request", {}),
        }
        payload = RuntimeCommand(
            command_id=identity,
            command_type="workflow.run",
            workspace_id=workspace,
            project_id=project,
            job_id=identity,
            workflow_run_id=identity,
            capability_snapshot_hash="0" * 64,
            trace_id=identity,
            idempotency_key=identity,
            payload=payload,
        ).model_dump(mode="json")
        control.execute(
            """INSERT INTO jobs(id,workspace_id,project_id,workflow_template_version,status,request,error)
                           VALUES(%s,%s,%s,'legacy.import@1',%s,%s,%s) ON CONFLICT DO NOTHING""",
            (identity, workspace, project, status, Jsonb(payload), Jsonb(error)),
        )
    for row in tables.get("run_job_events", []):
        if row.get("job_id") not in jobs:
            continue
        payload = decode(row).get("payload", {})
        identity = stable_id("event", str(row["job_id"]) + ":" + str(row["sequence"]))
        payload = RuntimeEvent(
            event_id=identity,
            event_type=row["event_type"],
            workspace_id=workspace,
            project_id=project,
            job_id=jobs[row["job_id"]],
            workflow_run_id=jobs[row["job_id"]],
            sequence=max(1, row["sequence"]),
            operation_id=identity,
            trace_id=identity,
            payload=payload,
        ).model_dump(mode="json")
        control.execute(
            """INSERT INTO runtime_event_inbox(event_id,workspace_id,job_id,sequence,event_type,payload,applied_at)
                           VALUES(%s,%s,%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING""",
            (identity, workspace, jobs[row["job_id"]], row["sequence"], row["event_type"], Jsonb(payload)),
        )
    metrics = [row for row in tables.get("model_call_metrics", []) if row.get("novel_id") == old_id]
    for index, row in enumerate(metrics):
        identity = stable_id("model-call", str(row.get("id", index)) + ":" + old_id)
        runtime.execute(
            """INSERT INTO model_calls(id,job_id,provider,model,prompt_hash,input_tokens,output_tokens,status,metadata)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                identity,
                import_job,
                row.get("provider", "legacy"),
                row.get("model_name", "legacy"),
                row.get("input_hash") or source_hash,
                row.get("input_tokens", 0),
                row.get("output_tokens", 0),
                "completed" if row.get("success") else "failed",
                Jsonb(
                    {
                        "legacy": True,
                        "agent": row.get("agent"),
                        "purpose": row.get("purpose"),
                        "fallback_used": bool(row.get("fallback_used")),
                    }
                ),
            ),
        )
    if metrics:
        inputs = sum(row.get("input_tokens", 0) for row in metrics)
        outputs = sum(row.get("output_tokens", 0) for row in metrics)
        record = {
            "id": str(uuid5(NAMESPACE_URL, "usage:" + project)),
            "input_tokens": inputs,
            "output_tokens": outputs,
            "total_tokens": inputs + outputs,
            "calls": len(metrics),
        }
        control.execute(
            """INSERT INTO project_records(id,workspace_id,project_id,kind,record_key,payload,content_hash)
                           VALUES(%s,%s,%s,'usage',%s,%s,%s) ON CONFLICT DO NOTHING""",
            (record["id"], workspace, project, record["id"], Jsonb(record), source_hash),
        )
    for row in tables.get("audit_logs", []):
        if row.get("tenant_id") and stable_id("workspace", row["tenant_id"]) != workspace:
            continue
        actor = row.get("actor_user_id")
        if not actor:
            continue
        control.execute(
            """INSERT INTO audit_logs(workspace_id,actor_id,action,resource_type,resource_id,metadata)
                           SELECT %s,%s,%s,%s,%s,%s WHERE NOT EXISTS(
                           SELECT 1 FROM audit_logs WHERE workspace_id=%s AND metadata->>'legacy_audit_id'=%s)""",
            (
                workspace,
                stable_id("user", actor),
                row.get("action", "legacy.import"),
                row.get("resource_type", "legacy"),
                stable_id("resource", row.get("resource_id") or "none"),
                Jsonb({"legacy_audit_id": str(row.get("id")), "legacy_metadata": decode(row).get("metadata", {})}),
                workspace,
                str(row.get("id")),
            ),
        )
