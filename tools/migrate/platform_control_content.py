"""Write imported Control content using generic immutable document versions."""

from uuid import NAMESPACE_URL, uuid5

from psycopg.types.json import Jsonb

from scripts.platform_legacy_export import stable_id


def import_brief(connection, workspace, project, version, payload, summary=""):
    document = stable_id("brief-document", project)
    connection.execute(
        """INSERT INTO documents(id,workspace_id,project_id,document_type,title)
           VALUES(%s,%s,%s,'brief','Project brief') ON CONFLICT DO NOTHING""",
        (document, workspace, project),
    )
    body = {"schema_version": "1.0", "blocks": [], "brief": payload, "change_summary": summary}
    connection.execute(
        """INSERT INTO document_versions(workspace_id,document_id,version,ir_schema_version,content_hash,payload,status)
           VALUES(%s,%s,%s,'1.0',encode(digest(convert_to(%s::jsonb::text,'UTF8'),'sha256'),'hex'),%s,'draft')
           ON CONFLICT DO NOTHING""",
        (workspace, document, version, Jsonb(body), Jsonb(body)),
    )
    connection.execute(
        """UPDATE documents SET current_version_id=(SELECT id FROM document_versions
           WHERE document_id=%s ORDER BY version DESC LIMIT 1) WHERE id=%s""",
        (document, document),
    )


def import_record(
    connection,
    workspace,
    project,
    identity,
    kind,
    key,
    index,
    version,
    artifact,
    record,
    content_hash,
    stale=False,
    baseline=False,
):
    if kind != "chapter":
        connection.execute(
            """INSERT INTO project_records(id,workspace_id,project_id,kind,record_key,block_index,
                       version_number,artifact_type,payload,content_hash,stale,baseline)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (
                identity,
                workspace,
                project,
                "audit" if kind == "book_audit" else kind,
                key,
                index,
                version,
                artifact,
                Jsonb(record),
                content_hash,
                stale,
                baseline,
            ),
        )
        return
    document = str(uuid5(NAMESPACE_URL, f"block-document:{project}:{index}"))
    connection.execute(
        """INSERT INTO documents(id,workspace_id,project_id,document_type,title)
           VALUES(%s,%s,%s,'block',%s) ON CONFLICT DO NOTHING""",
        (document, workspace, project, f"Block {index}"),
    )
    body = {
        "schema_version": "1.0",
        "blocks": [{"type": "text", "text": record.get("content", ""), "position": 0}],
        "record": record,
        "record_key": key,
        "record_hash": content_hash,
        "block_index": index,
    }
    connection.execute(
        """INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,
                                        content_hash,payload,status)
           VALUES(%s,%s,%s,%s,'1.0',encode(digest(convert_to(%s::jsonb::text,'UTF8'),'sha256'),'hex'),%s,'draft')
           ON CONFLICT DO NOTHING""",
        (identity, workspace, document, version, Jsonb(body), Jsonb(body)),
    )
    connection.execute(
        """INSERT INTO content_blocks(workspace_id,document_version_id,block_index,block_type,payload)
           VALUES(%s,%s,0,'text',%s) ON CONFLICT DO NOTHING""",
        (workspace, identity, Jsonb(body["blocks"][0])),
    )
    connection.execute(
        """UPDATE documents SET current_version_id=(SELECT id FROM document_versions
           WHERE document_id=%s ORDER BY version DESC LIMIT 1) WHERE id=%s""",
        (document, document),
    )
