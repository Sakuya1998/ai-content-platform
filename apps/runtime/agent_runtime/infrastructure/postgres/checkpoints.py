"""Official LangGraph saver interface backed by scoped, fenced PostgreSQL."""

import asyncio

from langgraph.checkpoint.base import WRITES_IDX_MAP, BaseCheckpointSaver, CheckpointTuple, get_checkpoint_metadata


class WorkflowPostgresSaver(BaseCheckpointSaver):
    def __init__(self, store):
        super().__init__()
        self.store = store

    def get_next_version(self, current, channel):
        value = int(current.split(".")[0]) if isinstance(current, str) else int(current or 0)
        return f"{value + 1:032}.0"

    def _scope(self, config):
        values = config.get("configurable", {})
        thread_id = values["thread_id"]
        if thread_id != self.store.thread_id:
            raise ValueError("checkpoint thread scope conflict")
        return thread_id, values.get("checkpoint_ns", ""), values.get("checkpoint_id")

    def _config(self, thread, namespace, checkpoint_id):
        return {"configurable": {"thread_id": thread, "checkpoint_ns": namespace, "checkpoint_id": checkpoint_id}}

    def get_tuple(self, config):
        thread, namespace, identity = self._scope(config)
        with self.store.transaction() as cursor:
            cursor.execute(
                """SELECT checkpoint_id,parent_checkpoint_id,checkpoint_type,checkpoint,metadata_type,metadata
                   FROM workflow_checkpoints WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s
                   AND thread_id=%s AND checkpoint_ns=%s
                   AND (%s::text IS NULL OR checkpoint_id=%s) ORDER BY checkpoint_id DESC LIMIT 1""",
                (*self.store.scope, thread, namespace, identity, identity),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            identity, parent, value_type, value, metadata_type, metadata = row
            cursor.execute(
                """SELECT task_id,channel,value_type,value FROM workflow_checkpoint_writes
                   WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s
                   AND thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s
                   ORDER BY task_id,write_index""",
                (*self.store.scope, thread, namespace, identity),
            )
            writes = [
                (task, channel, self.serde.loads_typed((kind, bytes(data))))
                for task, channel, kind, data in cursor.fetchall()
            ]
            return CheckpointTuple(
                self._config(thread, namespace, identity),
                self.serde.loads_typed((value_type, bytes(value))),
                self.serde.loads_typed((metadata_type, bytes(metadata))),
                self._config(thread, namespace, parent) if parent else None,
                writes,
            )

    def put(self, config, checkpoint, metadata, new_versions):
        thread, namespace, parent = self._scope(config)
        value_type, value = self.serde.dumps_typed(checkpoint)
        metadata_type, metadata_value = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        with self.store.transaction(write=True) as cursor:
            cursor.execute(
                """INSERT INTO workflow_checkpoints(workspace_id,project_id,workflow_id,thread_id,
                   checkpoint_ns,checkpoint_id,
                   parent_checkpoint_id,checkpoint_type,checkpoint,metadata_type,metadata,source_job_id,fencing_token)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    *self.store.scope,
                    thread,
                    namespace,
                    checkpoint["id"],
                    parent,
                    value_type,
                    value,
                    metadata_type,
                    metadata_value,
                    self.store.execution.job.command.job_id,
                    self.store.execution.job.fencing_token,
                ),
            )
        return self._config(thread, namespace, checkpoint["id"])

    def put_writes(self, config, writes, task_id, task_path=""):
        thread, namespace, identity = self._scope(config)
        with self.store.transaction(write=True) as cursor:
            for index, (channel, value) in enumerate(writes):
                index = WRITES_IDX_MAP.get(channel, index)
                value_type, data = self.serde.dumps_typed(value)
                conflict = (
                    (
                        "DO UPDATE SET value_type=excluded.value_type,value=excluded.value,"
                        "channel=excluded.channel,source_job_id=excluded.source_job_id,"
                        "fencing_token=excluded.fencing_token"
                    )
                    if index < 0
                    else "DO NOTHING"
                )
                cursor.execute(
                    """INSERT INTO workflow_checkpoint_writes(workspace_id,project_id,workflow_id,thread_id,
                       checkpoint_ns,
                       checkpoint_id,task_id,write_index,channel,value_type,value,task_path,source_job_id,fencing_token)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT
                       (workspace_id,project_id,workflow_id,thread_id,checkpoint_ns,checkpoint_id,
                        task_id,write_index) """
                    + conflict,
                    (
                        *self.store.scope,
                        thread,
                        namespace,
                        identity,
                        task_id,
                        index,
                        channel,
                        value_type,
                        data,
                        task_path,
                        self.store.execution.job.command.job_id,
                        self.store.execution.job.fencing_token,
                    ),
                )

    def list(self, config, *, filter=None, before=None, limit=None):
        config = config or {"configurable": {"thread_id": self.store.thread_id}}
        thread, namespace, _ = self._scope(config)
        cutoff = self._scope(before)[2] if before else None
        with self.store.transaction() as cursor:
            cursor.execute(
                """SELECT checkpoint_id FROM workflow_checkpoints
                   WHERE workspace_id=%s AND project_id=%s AND workflow_id=%s
                   AND thread_id=%s AND checkpoint_ns=%s AND (%s::text IS NULL OR checkpoint_id<%s)
                   ORDER BY checkpoint_id DESC""",
                (*self.store.scope, thread, namespace, cutoff, cutoff),
            )
            identities = [row[0] for row in cursor.fetchall()]
        count = 0
        for identity in identities:
            item = self.get_tuple(self._config(thread, namespace, identity))
            if filter and any(item.metadata.get(key) != value for key, value in filter.items()):
                continue
            if limit is not None and count >= limit:
                break
            count += 1
            yield item

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=""):
        await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    async def alist(self, config, *, filter=None, before=None, limit=None):
        items = await asyncio.to_thread(lambda: list(self.list(config, filter=filter, before=before, limit=limit)))
        for item in items:
            yield item


class NovelPostgresSaver(WorkflowPostgresSaver):
    """Name retained by the novel format adapter; storage is shared."""
