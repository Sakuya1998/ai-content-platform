-- A committed submitting marker fences external, non-idempotent task creation.
CREATE TABLE media_tasks (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    job_id text NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    request jsonb NOT NULL,
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    route_identity jsonb NOT NULL CHECK (NOT route_identity ? 'api_key'),
    review jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL CHECK (state IN ('prepared','submitting','submission_unknown','running',
        'succeeded','failed','cancel_pending','cancelled')),
    fencing_token bigint NOT NULL,
    remote_task_id text,
    content_url text,
    usage jsonb NOT NULL DEFAULT '{}'::jsonb,
    error jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id,job_id),
    CHECK (state NOT IN ('running','succeeded','cancel_pending') OR remote_task_id IS NOT NULL),
    CHECK (state != 'succeeded' OR content_url IS NOT NULL)
);

CREATE FUNCTION media_tasks_immutable_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
        OR NEW.project_id IS DISTINCT FROM OLD.project_id
        OR NEW.job_id IS DISTINCT FROM OLD.job_id
        OR NEW.request IS DISTINCT FROM OLD.request
        OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
        OR NEW.route_identity IS DISTINCT FROM OLD.route_identity
        OR (OLD.remote_task_id IS NOT NULL AND NEW.remote_task_id IS DISTINCT FROM OLD.remote_task_id)
    THEN
        RAISE EXCEPTION 'media task request, route and remote identity are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER media_tasks_immutable BEFORE UPDATE ON media_tasks
    FOR EACH ROW EXECUTE FUNCTION media_tasks_immutable_identity();

ALTER TABLE media_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE media_tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY media_tasks_scope ON media_tasks
    USING (
        workspace_id = current_setting('app.workspace_id',true)
        AND (NULLIF(current_setting('app.project_id',true),'') IS NULL
            OR project_id = current_setting('app.project_id',true))
        AND EXISTS (SELECT 1 FROM runtime_jobs j WHERE j.id=media_tasks.job_id
            AND j.workspace_id=media_tasks.workspace_id AND j.project_id=media_tasks.project_id)
    )
    WITH CHECK (
        workspace_id = current_setting('app.workspace_id',true)
        AND (NULLIF(current_setting('app.project_id',true),'') IS NULL
            OR project_id = current_setting('app.project_id',true))
        AND EXISTS (SELECT 1 FROM runtime_jobs j WHERE j.id=media_tasks.job_id
            AND j.workspace_id=media_tasks.workspace_id AND j.project_id=media_tasks.project_id)
    );
