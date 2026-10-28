"""Idempotently import a read-only legacy export into the two platform databases."""

import argparse
import base64
import hashlib
import json
from collections import defaultdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg
from agent_runtime.domain.content import ContentBlock, ContentDocument
from agent_runtime.domain.protocol import RuntimeCommand
from agent_runtime.infrastructure.postgres.novel import RECORD_CATEGORIES
from agent_runtime.workflows.novel.memory.hierarchical import build_hierarchical_memory
from dotenv import dotenv_values
from psycopg.types.json import Jsonb

from tools.migrate.platform_control_content import import_brief, import_record
from tools.migrate.platform_legacy_export import stable_id
from tools.migrate.platform_legacy_governance import import_governance
from tools.migrate.platform_legacy_history import import_history

RECORDS = {
    "chapter_versions": ("chapter", "chapter_versions"),
    "planning_versions": ("planning", "planning_versions"),
    "chapter_candidates": ("candidate", "candidates"),
    "chapter_evaluations": ("evaluation", "evaluations"),
    "book_audits": ("book_audit", "book_audits"),
    "memory_snapshots": ("memory", "memory_snapshots"),
    "memory_quality_runs": ("memory_quality", "memory_quality"),
    "model_call_metrics": ("trace", "traces"),
}


def decoded(row):
    result = dict(row)
    for name, value in row.items():
        if name.endswith("_json") and isinstance(value, str):
            try:
                result[name[:-5]] = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError("invalid legacy structured field") from None
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def checkpoint_bytes(value):
    return base64.b64decode(value["$binary"], validate=True) if isinstance(value, dict) else value


def remap_checkpoint(value, old_id, project):
    if isinstance(value, dict):
        return {
            key: project if key == "novel_id" and item == old_id else remap_checkpoint(item, old_id, project)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [remap_checkpoint(item, old_id, project) for item in value]
    if isinstance(value, tuple):
        return tuple(remap_checkpoint(item, old_id, project) for item in value)
    return value


def _runtime_job(connection, workspace, project, state, source_hash):
    identity = str(uuid5(NAMESPACE_URL, "legacy-job:" + project + ":" + source_hash))
    command = RuntimeCommand(
        command_id=identity,
        command_type="workflow.run",
        workspace_id=workspace,
        project_id=project,
        job_id=identity,
        workflow_run_id=identity,
        capability_snapshot_hash="0" * 64,
        trace_id=identity,
        idempotency_key=identity,
    )
    payload = command.model_dump(mode="json")
    connection.execute("INSERT INTO runtime_worker_scopes VALUES(%s) ON CONFLICT DO NOTHING", (workspace,))
    connection.execute(
        "INSERT INTO command_inbox(command_id,workspace_id,job_id,payload) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (identity, workspace, identity, Jsonb(payload)),
    )
    connection.execute(
        """INSERT INTO runtime_jobs(id,workspace_id,project_id,command_id,payload,status,result)
                       VALUES(%s,%s,%s,%s,%s,'completed',%s) ON CONFLICT DO NOTHING""",
        (identity, workspace, project, identity, Jsonb(payload), Jsonb({"legacy_import": source_hash})),
    )
    connection.execute(
        """INSERT INTO workflow_states(workspace_id,project_id,workflow_id,state,source_job_id,fencing_token)
                       VALUES(%s,%s,'novel.write@1',%s,%s,0) ON CONFLICT DO NOTHING""",
        (workspace, project, Jsonb(state), identity),
    )
    return identity


def import_bundle(bundle, control_url, runtime_url, *, dry_run=False, legacy_model_key=None, model_encryption_key=None):
    if bundle.get("schema_version") != "agent-platform-legacy-export@1":
        raise ValueError("unsupported legacy export")
    tables = bundle["tables"]
    source_hash = digest(bundle)
    report = {
        "source_hash": source_hash,
        "projects": len(tables.get("novels", [])),
        "users": len(tables.get("users", [])),
        "source_rows": sum(map(len, tables.values())),
        "warnings": list(bundle.get("warnings", [])),
    }
    if dry_run:
        return report
    with psycopg.connect(control_url) as connection:
        for novel in tables.get("novels", []):
            project = stable_id("project", novel["id"])
            workspace = stable_id("workspace", novel.get("tenant_id") or "tenant_local")
            connection.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
            existing = connection.execute(
                "SELECT metadata->>'legacy_import_hash' FROM projects WHERE id=%s", (project,)
            ).fetchone()
            if existing and existing[0] != source_hash:
                raise ValueError(
                    "target already contains a different legacy snapshot; use a fresh target "
                    "or explicit versioned import"
                )
    progress = {row["novel_id"]: decoded(row) for row in tables.get("progress", [])}
    chapters = defaultdict(list)
    for row in tables.get("chapters", []):
        chapter = decoded(row)
        chapter.update(chapter.get("digest") or {})
        chapters[row["novel_id"]].append(chapter)
    tenants = {row["id"]: row for row in tables.get("tenants", [])}
    for novel in tables.get("novels", []):
        tenants.setdefault(novel.get("tenant_id") or "tenant_local", {"name": "Imported workspace"})
    with psycopg.connect(control_url) as control:
        for tenant, row in tenants.items():
            workspace = stable_id("workspace", tenant)
            control.execute(
                "INSERT INTO workspaces(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (workspace, row.get("name") or "Imported workspace"),
            )
        for row in tables.get("users", []):
            workspace = stable_id("workspace", row.get("tenant_id") or "tenant_local")
            identity = stable_id("user", row["id"])
            control.execute(
                """INSERT INTO users(id,workspace_id,email,username,display_name,password_hash,role)
                            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                (
                    identity,
                    workspace,
                    row.get("email") or identity + "@local.invalid",
                    row.get("username") or identity,
                    row.get("display_name") or row.get("username") or "Imported user",
                    row.get("password_hash"),
                    row.get("role") or "viewer",
                ),
            )
        import_governance(
            control, tables, decoded, digest, report, legacy_key=legacy_model_key, encryption_key=model_encryption_key
        )
    for original in tables.get("novels", []):
        old_id = original["id"]
        workspace, project = (
            stable_id("workspace", original.get("tenant_id") or "tenant_local"),
            stable_id("project", old_id),
        )
        document_id = stable_id("document", old_id)
        novel = decoded(original)
        values = progress.get(old_id, {})
        state = remap_checkpoint(values.get("state") or {}, old_id, project)
        final_chapters = sorted(chapters[old_id], key=lambda row: row["chapter_number"])
        state = {
            **state,
            "novel_id": project,
            "title": novel["title"],
            "chapters": state.get("chapters") or final_chapters,
        }
        state.setdefault(
            "memory_index", build_hierarchical_memory(state["chapters"], total_chapters=novel.get("total_chapters", 0))
        )
        input_data = {
            key: novel.get(key)
            for key in [
                "title",
                "genre",
                "style",
                "inspiration",
                "total_chapters",
                "planning_review_enabled",
                "creative_brief",
                "creative_brief_version",
                "content_type_snapshot",
                "style_snapshot",
                "creative_template_snapshot",
                "quality_policy_snapshot",
                "primary_type_id",
                "resource_tags",
                "style_resource_id",
                "creative_template_id",
                "quality_policy_id",
            ]
            if novel.get(key) is not None
        }
        input_data.setdefault("creative_brief", {})
        for key in ["primary_type_id", "style_resource_id", "creative_template_id", "quality_policy_id"]:
            value = input_data.get(key)
            if value and not str(value).startswith(("system:", "system_")):
                input_data[key] = stable_id("resource", value)
        state_hash = digest(state)
        with psycopg.connect(control_url) as control:
            control.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
            control.execute(
                """INSERT INTO projects(id,workspace_id,project_type,name,metadata)
                   VALUES(%s,%s,'novel',%s,%s) ON CONFLICT DO NOTHING""",
                (project, workspace, novel["title"], Jsonb({"legacy_id": old_id, "legacy_import_hash": source_hash})),
            )
            control.execute(
                """INSERT INTO documents(id,workspace_id,project_id,document_type,title)
                   VALUES(%s,%s,%s,'novel',%s) ON CONFLICT DO NOTHING""",
                (document_id, workspace, project, novel["title"]),
            )
            control.execute(
                """INSERT INTO project_projections(id,workspace_id,document_id,input,state,state_hash,brief_version)
                            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    project,
                    workspace,
                    document_id,
                    Jsonb(
                        {
                            "title": novel["title"],
                            "content_type": "novel",
                            "input": input_data,
                            "brief": input_data["creative_brief"],
                        }
                    ),
                    Jsonb(state),
                    state_hash,
                    novel.get("creative_brief_version", 1),
                ),
            )
            import_brief(
                control, workspace, project, novel.get("creative_brief_version", 1), input_data["creative_brief"]
            )
            for row in tables.get("creative_brief_versions", []):
                if row.get("novel_id") == old_id:
                    brief = decoded(row)
                    import_brief(
                        control,
                        workspace,
                        project,
                        row["version_number"],
                        brief.get("brief") or brief.get("creative_brief") or {},
                        row.get("change_summary", ""),
                    )
            if final_chapters:
                ir = ContentDocument(
                    document_id=document_id,
                    project_id=project,
                    document_type="novel",
                    version=1,
                    schema_version="1.0",
                    blocks=[
                        ContentBlock(
                            type="chapter",
                            text=row.get("content", ""),
                            position=index,
                            metadata={"chapter_number": row["chapter_number"], "title": row.get("title", "")},
                        )
                        for index, row in enumerate(final_chapters)
                    ],
                )
                version_id = stable_id("document_version", old_id)
                control.execute(
                    """INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,
                                content_hash,payload,status) VALUES(%s,%s,%s,1,'1.0',%s,%s,'approved')
                                ON CONFLICT DO NOTHING""",
                    (version_id, workspace, document_id, ir.content_hash, Jsonb(ir.model_dump(mode="json"))),
                )
                control.execute(
                    "UPDATE documents SET current_version_id=%s WHERE id=%s AND current_version_id IS NULL",
                    (version_id, document_id),
                )
                for block in ir.blocks:
                    control.execute(
                        """INSERT INTO content_blocks(workspace_id,document_version_id,block_index,block_type,payload)
                           VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        (workspace, version_id, block.position, block.type, Jsonb(block.model_dump(mode="json"))),
                    )
        with psycopg.connect(runtime_url) as runtime:
            runtime.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
            job_id = _runtime_job(runtime, workspace, project, state, source_hash)
            for table, (control_kind, runtime_kind) in RECORDS.items():
                for index, row in enumerate(tables.get(table, [])):
                    if row.get("novel_id") != old_id:
                        continue
                    record = decoded(row)
                    identity = stable_id("record:" + table, str(row.get("id", index)) + ":" + old_id)
                    legacy_record_id = str(row.get("id", identity))
                    record.update(id=legacy_record_id, novel_id=project)
                    chapter = int(row.get("chapter_number", 0) or 0)
                    version = int(row.get("version_number", index + 1) or index + 1)
                    record.update(chapter_number=chapter, version_number=version)
                    content_hash = digest(record)
                    runtime.execute(
                        """INSERT INTO workflow_records(workspace_id,project_id,workflow_id,category,
                                    record_kind,record_id,scope_key,
                                    version_number,content_hash,payload,source_job_id,fencing_token)
                                    VALUES(%s,%s,'novel.write@1',%s,%s,%s,%s,%s,%s,%s,%s,0) ON CONFLICT DO NOTHING""",
                        (
                            workspace,
                            project,
                            RECORD_CATEGORIES.get(runtime_kind, "execution_event"),
                            runtime_kind,
                            legacy_record_id,
                            str(chapter),
                            version,
                            content_hash,
                            Jsonb(record),
                            job_id,
                        ),
                    )
                    with psycopg.connect(control_url) as control:
                        control.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
                        control_record = {**record, "project_id": project, "block_index": chapter}
                        import_record(
                            control,
                            workspace,
                            project,
                            identity,
                            control_kind,
                            legacy_record_id,
                            chapter,
                            version,
                            row.get("artifact_type", ""),
                            control_record,
                            digest(control_record),
                            bool(row.get("is_stale", False)),
                            bool(row.get("is_baseline", False)),
                        )
            _import_checkpoints(runtime, bundle.get("checkpoints", {}), old_id, workspace, project, job_id)
            _import_vectors(runtime, bundle.get("chroma", {}), old_id, workspace, project, job_id, report)
            with psycopg.connect(control_url) as control:
                control.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
                import_history(control, runtime, tables, decoded, workspace, project, old_id, job_id, source_hash)
    _archive_source_rows(tables, source_hash, control_url)
    with psycopg.connect(control_url) as connection:
        archived = 0
        for workspace in {stable_id("workspace", tenant) for tenant in tenants}:
            connection.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
            archived += connection.execute(
                "SELECT count(*) FROM legacy_import_rows WHERE source_hash=%s", (source_hash,)
            ).fetchone()[0]
        report["archived_rows"] = archived
        verified = 0
        for row in tables.get("novels", []):
            workspace = stable_id("workspace", row.get("tenant_id") or "tenant_local")
            connection.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
            target = connection.execute(
                """SELECT p.metadata->>'legacy_import_hash',n.state_hash FROM projects p
                                      JOIN project_projections n ON n.id=p.id WHERE p.id=%s""",
                (stable_id("project", row["id"]),),
            ).fetchone()
            if not target or target[0] != source_hash or not target[1]:
                raise ValueError("target reconciliation failed")
            verified += 1
        report["target_projects_verified"] = verified
    return report


def _import_checkpoints(connection, tables, old_id, workspace, project, job_id):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serializer = JsonPlusSerializer()
    for row in tables.get("checkpoints", []):
        if row.get("thread_id") != old_id:
            continue
        checkpoint = remap_checkpoint(
            serializer.loads_typed((row["type"], checkpoint_bytes(row["checkpoint"]))), old_id, project
        )
        values = checkpoint.get("channel_values", {})
        if values.get("novel_id") == old_id:
            values["novel_id"] = project
        kind, body = serializer.dumps_typed(checkpoint)
        metadata_value = checkpoint_bytes(row.get("metadata"))
        metadata = json.loads(metadata_value) if isinstance(metadata_value, (str, bytes)) else metadata_value or {}
        metadata_kind, metadata_body = serializer.dumps_typed(metadata)
        connection.execute(
            """INSERT INTO workflow_checkpoints(workspace_id,project_id,workflow_id,thread_id,checkpoint_ns,
                           checkpoint_id,parent_checkpoint_id,checkpoint_type,checkpoint,metadata_type,metadata,
                           source_job_id,fencing_token) VALUES(%s,%s,'novel.write@1',%s,%s,%s,%s,%s,%s,%s,%s,%s,0)
                           ON CONFLICT DO NOTHING""",
            (
                workspace,
                project,
                project,
                row.get("checkpoint_ns", ""),
                row["checkpoint_id"],
                row.get("parent_checkpoint_id"),
                kind,
                body,
                metadata_kind,
                metadata_body,
                job_id,
            ),
        )
    for row in tables.get("writes", []):
        if row.get("thread_id") != old_id:
            continue
        value = serializer.loads_typed((row["type"], checkpoint_bytes(row["value"])))
        value = (
            project if row["channel"] == "novel_id" and value == old_id else remap_checkpoint(value, old_id, project)
        )
        kind, body = serializer.dumps_typed(value)
        connection.execute(
            """INSERT INTO workflow_checkpoint_writes(workspace_id,project_id,workflow_id,thread_id,checkpoint_ns,
                           checkpoint_id,task_id,write_index,channel,value_type,value,task_path,source_job_id,fencing_token)
                           VALUES(%s,%s,'novel.write@1',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0) ON CONFLICT DO NOTHING""",
            (
                workspace,
                project,
                project,
                row.get("checkpoint_ns", ""),
                row["checkpoint_id"],
                row["task_id"],
                row["idx"],
                row["channel"],
                kind,
                body,
                row.get("task_path", ""),
                job_id,
            ),
        )


def _import_vectors(connection, collections, old_id, workspace, project, job_id, report):
    collection = collections.get("novel_" + old_id, {})
    for index, identity in enumerate(collection.get("ids", [])):
        warning = "Legacy vector model provenance is unknown; rebuild memory before semantic retrieval."
        if warning not in report["warnings"]:
            report["warnings"].append(warning)
        embeddings = collection.get("embeddings") or []
        if index >= len(embeddings) or len(embeddings[index]) != 1536:
            warning = "Legacy embeddings with incompatible dimensions require a memory rebuild."
            if warning not in report["warnings"]:
                report["warnings"].append(warning)
            continue
        metadata = (collection.get("metadatas") or [{}])[index] or {}
        memory_id = str(identity).replace(old_id, project)
        node_id = str(uuid5(NAMESPACE_URL, f"legacy-memory:{workspace}:{project}:{memory_id}"))
        content = collection["documents"][index]
        connection.execute(
            """INSERT INTO memory_nodes(id,workspace_id,project_id,workflow_id,memory_type,source_id,source_version,
                           content,metadata,content_hash,source_job_id,fencing_token)
                           VALUES(%s,%s,%s,'novel.write@1',%s,%s,1,%s,%s,%s,%s,0) ON CONFLICT DO NOTHING""",
            (
                node_id,
                workspace,
                project,
                metadata.get("type", "summary"),
                memory_id,
                content,
                Jsonb(metadata),
                hashlib.sha256(content.encode()).hexdigest(),
                job_id,
            ),
        )
        connection.execute(
            """INSERT INTO memory_embeddings(id,project_id,embedding_model,index_version,embedding)
               VALUES(%s,%s,%s,%s,%s::vector) ON CONFLICT DO NOTHING""",
            (
                node_id,
                project,
                metadata.get("embedding_model", "unknown"),
                metadata.get("index_version", "unknown"),
                json.dumps(embeddings[index]),
            ),
        )


def _archive_source_rows(tables, source_hash, url):
    novel_tenants = {row["id"]: row.get("tenant_id") or "tenant_local" for row in tables.get("novels", [])}
    job_tenants = {row["id"]: novel_tenants.get(row["novel_id"], "tenant_local") for row in tables.get("run_jobs", [])}
    with psycopg.connect(url) as connection:
        for table, rows in tables.items():
            for index, row in enumerate(rows):
                tenant = (
                    row.get("tenant_id")
                    or novel_tenants.get(row.get("novel_id"))
                    or job_tenants.get(row.get("job_id"))
                    or (row.get("id") if table == "tenants" else "tenant_local")
                )
                workspace = stable_id("workspace", tenant)
                exists = connection.execute("SELECT 1 FROM workspaces WHERE id=%s", (workspace,)).fetchone()
                if not exists:
                    continue
                connection.execute("SELECT set_config('app.workspace_id',%s,true)", (workspace,))
                connection.execute(
                    """INSERT INTO legacy_import_rows(source_hash,source_table,source_key,workspace_id,payload)
                                   VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                    (source_hash, table, str(index) + ":" + digest(row), workspace, Jsonb(row)),
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--control-env", default=".env.control.external")
    parser.add_argument("--runtime-env", default=".env.runtime.external")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--legacy-model-key-file")
    args = parser.parse_args()
    bundle = json.loads(Path(args.path).read_text())
    control, runtime = dotenv_values(args.control_env), dotenv_values(args.runtime_env)
    print(
        json.dumps(
            import_bundle(
                bundle,
                control["CONTROL_DATABASE_URL"],
                runtime["RUNTIME_DATABASE_URL"],
                dry_run=args.dry_run,
                legacy_model_key=Path(args.legacy_model_key_file).read_bytes() if args.legacy_model_key_file else None,
                model_encryption_key=control.get("CONTROL_PLANE_SECURITY_MODEL_ENCRYPTION_KEY"),
            )
        )
    )


if __name__ == "__main__":
    main()
