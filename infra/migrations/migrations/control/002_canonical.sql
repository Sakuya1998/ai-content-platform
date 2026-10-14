-- Canonical Control schema. The platform owns this schema; Go must not carry
-- a second control_* table family.
DROP TABLE IF EXISTS control_job_events, control_outbox, control_inbox,
    control_idempotency_keys, control_versions, control_documents,
    control_projects, control_workspace_members, control_users, control_workspaces CASCADE;

ALTER TABLE projects ADD COLUMN IF NOT EXISTS description text NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_project_type_check;
ALTER TABLE projects ADD CONSTRAINT projects_project_type_check
    CHECK (project_type IN ('novel','screenplay','copy','short_drama','advertisement','video','generic'));
ALTER TABLE command_outbox ALTER COLUMN job_id DROP NOT NULL;

CREATE TABLE IF NOT EXISTS idempotency_keys (
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    operation text NOT NULL,
    idempotency_key text NOT NULL,
    payload_hash bytea NOT NULL,
    status_code integer NOT NULL,
    response jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, operation, idempotency_key)
);

CREATE TABLE IF NOT EXISTS control_audit_logs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    actor_id uuid,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
