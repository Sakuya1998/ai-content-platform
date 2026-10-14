ALTER TABLE runtime_jobs ADD COLUMN resume_input jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE memory_version_status (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    document_version_id uuid NOT NULL,
    job_id text NOT NULL REFERENCES runtime_jobs(id),
    status text NOT NULL CHECK(status='completed'),
    completed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id,project_id,document_version_id)
);
ALTER TABLE memory_version_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_version_status FORCE ROW LEVEL SECURITY;
CREATE POLICY memory_version_scope ON memory_version_status
    USING(workspace_id=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id=current_setting('app.workspace_id',true));
