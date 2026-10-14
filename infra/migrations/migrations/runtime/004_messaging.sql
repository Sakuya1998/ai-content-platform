ALTER TABLE runtime_jobs ADD COLUMN task_queue text NOT NULL DEFAULT 'runtime';
ALTER TABLE runtime_jobs ADD CONSTRAINT runtime_jobs_queue_check CHECK(task_queue IN ('runtime','memory'));
ALTER TABLE runtime_jobs ADD COLUMN attempt integer NOT NULL DEFAULT 0;
ALTER TABLE runtime_jobs ADD COLUMN next_retry_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE runtime_event_outbox ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX runtime_events_pending_idx ON runtime_event_outbox(next_attempt_at,id) WHERE published_at IS NULL;
CREATE INDEX runtime_jobs_due_idx ON runtime_jobs(task_queue,next_retry_at,created_at);

-- Worker discovery contains only workspace IDs, never tenant payloads. Every
-- subsequent operation establishes app.workspace_id and remains subject to RLS.
CREATE TABLE runtime_worker_scopes (workspace_id text PRIMARY KEY);
INSERT INTO runtime_worker_scopes SELECT DISTINCT workspace_id FROM runtime_jobs;

CREATE TABLE runtime_cancellations (
    workspace_id text NOT NULL,
    job_id text NOT NULL,
    project_id text NOT NULL,
    command_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id,job_id)
);
CREATE TABLE memory_event_inbox (
    event_id text PRIMARY KEY,
    workspace_id text NOT NULL,
    payload jsonb NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE runtime_command_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    command_id text NOT NULL UNIQUE,
    workspace_id text NOT NULL,
    topic text NOT NULL,
    payload jsonb NOT NULL,
    published_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX runtime_commands_pending_idx ON runtime_command_outbox(next_attempt_at,id) WHERE published_at IS NULL;
ALTER TABLE runtime_cancellations ENABLE ROW LEVEL SECURITY;
ALTER TABLE runtime_cancellations FORCE ROW LEVEL SECURITY;
CREATE POLICY cancellations_scope ON runtime_cancellations
    USING (workspace_id = current_setting('app.workspace_id',true))
    WITH CHECK (workspace_id = current_setting('app.workspace_id',true));
ALTER TABLE memory_event_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_event_inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY memory_events_scope ON memory_event_inbox
    USING (workspace_id = current_setting('app.workspace_id',true))
    WITH CHECK (workspace_id = current_setting('app.workspace_id',true));
ALTER TABLE runtime_command_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE runtime_command_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY runtime_commands_scope ON runtime_command_outbox
    USING (workspace_id = current_setting('app.workspace_id',true))
    WITH CHECK (workspace_id = current_setting('app.workspace_id',true));
