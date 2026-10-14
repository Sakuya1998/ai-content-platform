ALTER TABLE runtime_event_inbox ALTER COLUMN event_id TYPE text USING event_id::text;
ALTER TABLE runtime_event_inbox DROP CONSTRAINT runtime_event_inbox_job_id_fkey;
ALTER TABLE runtime_event_inbox ALTER COLUMN job_id TYPE text USING job_id::text;
ALTER TABLE runtime_event_inbox ADD COLUMN applied_at timestamptz;
ALTER TABLE jobs ADD COLUMN last_event_sequence bigint NOT NULL DEFAULT 0;
ALTER TABLE command_outbox ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX command_outbox_pending_idx ON command_outbox(next_attempt_at,id) WHERE published_at IS NULL;
ALTER TABLE command_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE command_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY command_outbox_workspace_policy ON command_outbox
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
ALTER TABLE runtime_event_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE runtime_event_inbox FORCE ROW LEVEL SECURITY;
CREATE POLICY runtime_event_inbox_workspace_policy ON runtime_event_inbox
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
