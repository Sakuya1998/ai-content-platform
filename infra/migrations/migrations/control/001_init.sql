-- Control DB: management-plane authority and query projections.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS workspaces (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email text NOT NULL,
    role text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, email)
);

CREATE TABLE IF NOT EXISTS projects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    project_type text NOT NULL CHECK (project_type IN ('novel', 'screenplay', 'copy', 'short_drama', 'advertisement', 'video')),
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    document_type text NOT NULL,
    title text NOT NULL,
    current_version_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, project_id, title)
);

CREATE TABLE IF NOT EXISTS document_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version integer NOT NULL CHECK (version > 0),
    ir_schema_version text NOT NULL,
    content_hash text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('draft', 'approved', 'archived')),
    created_by uuid REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, version),
    UNIQUE (document_id, content_hash)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'documents_current_version_fk'
    ) THEN
        ALTER TABLE documents ADD CONSTRAINT documents_current_version_fk
            FOREIGN KEY (current_version_id) REFERENCES document_versions(id)
            DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS content_blocks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    document_version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
    block_index integer NOT NULL CHECK (block_index >= 0),
    block_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_ref jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (document_version_id, block_index)
);

CREATE TABLE IF NOT EXISTS derivations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_version_id uuid NOT NULL REFERENCES document_versions(id),
    target_version_id uuid NOT NULL REFERENCES document_versions(id),
    workflow_run_id uuid,
    workflow_template_version text NOT NULL,
    source_ir_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_version_id, target_version_id)
);

CREATE TABLE IF NOT EXISTS workflow_templates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid REFERENCES workspaces(id) ON DELETE CASCADE,
    name text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL CHECK (status IN ('draft', 'published', 'disabled')),
    definition jsonb NOT NULL,
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, name, version)
);

CREATE TABLE IF NOT EXISTS capability_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    content_hash text NOT NULL,
    snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, content_hash)
);

CREATE TABLE IF NOT EXISTS capability_definitions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid REFERENCES workspaces(id) ON DELETE CASCADE,
    parent_id uuid REFERENCES capability_definitions(id),
    kind text NOT NULL CHECK (kind IN ('prompt_pack', 'skill', 'tool', 'mcp_policy')),
    key text NOT NULL,
    version integer NOT NULL CHECK (version > 0),
    status text NOT NULL CHECK (status IN ('draft', 'published', 'disabled')),
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, kind, key, version)
);

CREATE TABLE IF NOT EXISTS media_assets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source_version_id uuid REFERENCES document_versions(id),
    object_key text NOT NULL,
    media_type text NOT NULL,
    content_hash text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, object_key, content_hash)
);

CREATE TABLE IF NOT EXISTS jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    workflow_template_version text NOT NULL,
    capability_snapshot_id uuid REFERENCES capability_snapshots(id),
    status text NOT NULL CHECK (status IN ('queued', 'running', 'waiting_review', 'completed', 'failed', 'cancel_requested', 'cancelled')),
    state_version bigint NOT NULL DEFAULT 1,
    request jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    error jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS jobs_workspace_status_idx ON jobs (workspace_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS document_versions_document_idx ON document_versions (document_id, version DESC);
CREATE INDEX IF NOT EXISTS content_blocks_version_idx ON content_blocks (document_version_id, block_index);

CREATE TABLE IF NOT EXISTS command_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    command_id uuid NOT NULL UNIQUE,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    topic text NOT NULL,
    payload jsonb NOT NULL,
    published_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runtime_event_inbox (
    event_id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    sequence bigint NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, sequence)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    actor_id uuid,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE capability_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE media_assets ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS projects_workspace_policy ON projects;
CREATE POLICY projects_workspace_policy ON projects
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
DROP POLICY IF EXISTS documents_workspace_policy ON documents;
CREATE POLICY documents_workspace_policy ON documents
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
DROP POLICY IF EXISTS document_versions_workspace_policy ON document_versions;
CREATE POLICY document_versions_workspace_policy ON document_versions
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
DROP POLICY IF EXISTS jobs_workspace_policy ON jobs;
CREATE POLICY jobs_workspace_policy ON jobs
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
DROP POLICY IF EXISTS capability_definitions_workspace_policy ON capability_definitions;
CREATE POLICY capability_definitions_workspace_policy ON capability_definitions
    USING (workspace_id IS NULL OR workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id IS NULL OR workspace_id::text = current_setting('app.workspace_id', true));
DROP POLICY IF EXISTS media_assets_workspace_policy ON media_assets;
CREATE POLICY media_assets_workspace_policy ON media_assets
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));

ALTER TABLE projects FORCE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE document_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE capability_definitions FORCE ROW LEVEL SECURITY;
ALTER TABLE media_assets FORCE ROW LEVEL SECURITY;
