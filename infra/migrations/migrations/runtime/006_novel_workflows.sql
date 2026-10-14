CREATE TABLE novel_projects (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_job_id text NOT NULL REFERENCES runtime_jobs(id),
    fencing_token bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id,project_id)
);
CREATE TABLE novel_records (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    kind text NOT NULL,
    record_id text NOT NULL,
    chapter_number integer NOT NULL DEFAULT 0,
    version_number integer NOT NULL,
    content_hash text NOT NULL,
    payload jsonb NOT NULL,
    source_job_id text NOT NULL REFERENCES runtime_jobs(id),
    fencing_token bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id,project_id,kind,record_id),
    UNIQUE(workspace_id,project_id,kind,chapter_number,version_number)
);
CREATE TABLE novel_checkpoints (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL,
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    checkpoint_type text NOT NULL,
    checkpoint bytea NOT NULL,
    metadata_type text NOT NULL,
    metadata bytea NOT NULL,
    source_job_id text NOT NULL REFERENCES runtime_jobs(id),
    fencing_token bigint NOT NULL,
    PRIMARY KEY(workspace_id,project_id,thread_id,checkpoint_ns,checkpoint_id)
);
CREATE TABLE novel_checkpoint_writes (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    write_index integer NOT NULL,
    channel text NOT NULL,
    value_type text NOT NULL,
    value bytea NOT NULL,
    task_path text NOT NULL DEFAULT '',
    source_job_id text NOT NULL REFERENCES runtime_jobs(id),
    fencing_token bigint NOT NULL,
    PRIMARY KEY(workspace_id,project_id,thread_id,checkpoint_ns,checkpoint_id,task_id,write_index)
);
CREATE TABLE novel_memories (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    memory_id text NOT NULL,
    content text NOT NULL,
    metadata jsonb NOT NULL,
    embedding vector(1536) NOT NULL,
    source_job_id text NOT NULL REFERENCES runtime_jobs(id),
    source_version_id text,
    fencing_token bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id,project_id,memory_id)
);
CREATE INDEX novel_record_lookup ON novel_records(workspace_id,project_id,kind,chapter_number,version_number);
ALTER TABLE novel_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE novel_projects FORCE ROW LEVEL SECURITY;
CREATE POLICY novel_projects_scope ON novel_projects USING(workspace_id=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id=current_setting('app.workspace_id',true));
ALTER TABLE novel_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE novel_records FORCE ROW LEVEL SECURITY;
CREATE POLICY novel_records_scope ON novel_records USING(workspace_id=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id=current_setting('app.workspace_id',true));
ALTER TABLE novel_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE novel_checkpoints FORCE ROW LEVEL SECURITY;
CREATE POLICY novel_checkpoints_scope ON novel_checkpoints USING(workspace_id=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id=current_setting('app.workspace_id',true));
ALTER TABLE novel_checkpoint_writes ENABLE ROW LEVEL SECURITY;
ALTER TABLE novel_checkpoint_writes FORCE ROW LEVEL SECURITY;
CREATE POLICY novel_writes_scope ON novel_checkpoint_writes USING(workspace_id=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id=current_setting('app.workspace_id',true));
ALTER TABLE novel_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE novel_memories FORCE ROW LEVEL SECURITY;
CREATE POLICY novel_memories_scope ON novel_memories USING(workspace_id=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id=current_setting('app.workspace_id',true));
