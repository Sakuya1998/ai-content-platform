ALTER TABLE document_versions ADD COLUMN source_job_id uuid REFERENCES jobs(id);
CREATE UNIQUE INDEX document_versions_source_job_idx ON document_versions(source_job_id) WHERE source_job_id IS NOT NULL;
ALTER TABLE capability_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE capability_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY capability_snapshots_scope ON capability_snapshots
    USING(workspace_id::text=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id::text=current_setting('app.workspace_id',true));
