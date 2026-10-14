-- Derived generic Canon views share the source transaction and have no independent write API.
CREATE TABLE canonical_snapshots (
 workspace_id text NOT NULL,
 project_id text NOT NULL,
 content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
 schema_version text NOT NULL DEFAULT 'canon@1',
 payload jsonb NOT NULL,
 policy_version_id text NOT NULL,
 source_job_id text NOT NULL REFERENCES runtime_jobs(id),
 fencing_token bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,project_id,content_hash)
);
ALTER TABLE canonical_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_snapshots FORCE ROW LEVEL SECURITY;
CREATE POLICY canonical_snapshots_scope ON canonical_snapshots
 USING(workspace_id=current_setting('app.workspace_id',true))
 WITH CHECK(workspace_id=current_setting('app.workspace_id',true));
CREATE FUNCTION reject_canonical_snapshot_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'canonical snapshots are immutable'; END $$;
CREATE TRIGGER canonical_snapshots_immutable BEFORE UPDATE OR DELETE ON canonical_snapshots
 FOR EACH ROW EXECUTE FUNCTION reject_canonical_snapshot_mutation();
