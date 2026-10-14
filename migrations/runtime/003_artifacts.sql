CREATE TABLE artifact_records (
    job_id text NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    workspace_id text NOT NULL,
    bucket text NOT NULL,
    object_key text NOT NULL,
    sha256 text NOT NULL CHECK (length(sha256) = 64),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    reference jsonb NOT NULL,
    event_id text NOT NULL REFERENCES runtime_event_outbox(event_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, bucket, object_key)
);
ALTER TABLE artifact_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_records FORCE ROW LEVEL SECURITY;
CREATE POLICY artifact_records_workspace_policy ON artifact_records
    USING (workspace_id = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true));
