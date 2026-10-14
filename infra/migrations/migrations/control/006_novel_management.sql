-- Novel query projections are updated only by ordered Runtime events.
CREATE TABLE novels (
 id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 input jsonb NOT NULL,
 state jsonb NOT NULL DEFAULT '{}'::jsonb,
 state_hash text NOT NULL DEFAULT '',
 brief_version integer NOT NULL DEFAULT 1 CHECK(brief_version>0),
 revision bigint NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE novel_brief_versions (
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 novel_id uuid NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
 version integer NOT NULL CHECK(version>0),
 payload jsonb NOT NULL,
 change_summary text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(novel_id,version)
);
CREATE TABLE novel_records (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 novel_id uuid NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
 kind text NOT NULL CHECK(kind IN ('chapter','planning','candidate','evaluation','book_audit','memory','memory_quality','usage','trace','canon','conflict')),
 record_key text NOT NULL,
 chapter_number integer NOT NULL DEFAULT 0 CHECK(chapter_number>=0),
 version_number integer NOT NULL DEFAULT 0 CHECK(version_number>=0),
 artifact_type text NOT NULL DEFAULT '',
 payload jsonb NOT NULL,
 content_hash text NOT NULL,
 stale boolean NOT NULL DEFAULT false,
 baseline boolean NOT NULL DEFAULT false,
 source_job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(novel_id,kind,record_key)
);
CREATE INDEX novel_records_query_idx ON novel_records(novel_id,kind,chapter_number,version_number DESC);
CREATE UNIQUE INDEX novel_evaluation_baseline_idx ON novel_records(novel_id,chapter_number) WHERE kind='evaluation' AND baseline;
CREATE TABLE novel_request_keys (
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 key text NOT NULL CHECK(length(key) BETWEEN 1 AND 200),
 fingerprint text NOT NULL,
 novel_id uuid NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(workspace_id,key)
);
CREATE TABLE novel_transfers (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 novel_id uuid NOT NULL REFERENCES novels(id) ON DELETE CASCADE,
 status text NOT NULL CHECK(status IN ('queued','running','completed','cancelled','failed')),
 format text NOT NULL CHECK(format IN ('backup','markdown','txt','docx','epub')),
 result bytea,
 content_hash text,
 error text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
-- Waiting review is resumable: a resume command is a new job, while queued/running
-- jobs exclude every other novel mutation until cancellation has finished.
CREATE UNIQUE INDEX jobs_single_active_novel_idx ON jobs(project_id)
 WHERE status IN ('queued','running','cancel_requested') AND request->'payload'->>'document_type'='novel';
DO $$
DECLARE item text;
BEGIN
 FOREACH item IN ARRAY ARRAY['novels','novel_brief_versions','novel_records','novel_request_keys','novel_transfers'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',item);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',item);
  EXECUTE format('CREATE POLICY %I ON %I USING (workspace_id::text=current_setting(''app.workspace_id'',true)) WITH CHECK (workspace_id::text=current_setting(''app.workspace_id'',true))',item||'_workspace_policy',item);
 END LOOP;
END $$;
