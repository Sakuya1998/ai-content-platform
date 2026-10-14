CREATE TABLE composition_runs (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    job_id text NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    request jsonb NOT NULL,
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    state text NOT NULL CHECK (state IN ('prepared','rendering','cancel_requested','cancelled','outputs_ready')),
    fencing_token bigint NOT NULL,
    artifact jsonb,
    probe jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id,job_id),
    CHECK(state!='outputs_ready' OR artifact IS NOT NULL)
);
CREATE FUNCTION composition_identity_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id OR NEW.project_id IS DISTINCT FROM OLD.project_id
        OR NEW.job_id IS DISTINCT FROM OLD.job_id OR NEW.request IS DISTINCT FROM OLD.request
        OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
        OR (OLD.artifact IS NOT NULL AND (NEW.artifact IS DISTINCT FROM OLD.artifact
            OR NEW.probe IS DISTINCT FROM OLD.probe)) THEN
        RAISE EXCEPTION 'composition identity and archived result are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER composition_identity BEFORE UPDATE ON composition_runs
    FOR EACH ROW EXECUTE FUNCTION composition_identity_immutable();
ALTER TABLE composition_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE composition_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY composition_scope ON composition_runs
    USING(workspace_id=current_setting('app.workspace_id',true)
        AND (NULLIF(current_setting('app.project_id',true),'') IS NULL
            OR project_id=current_setting('app.project_id',true))
        AND EXISTS(SELECT 1 FROM runtime_jobs j WHERE j.id=composition_runs.job_id
            AND j.workspace_id=composition_runs.workspace_id AND j.project_id=composition_runs.project_id))
    WITH CHECK(workspace_id=current_setting('app.workspace_id',true)
        AND (NULLIF(current_setting('app.project_id',true),'') IS NULL
            OR project_id=current_setting('app.project_id',true))
        AND EXISTS(SELECT 1 FROM runtime_jobs j WHERE j.id=composition_runs.job_id
            AND j.workspace_id=composition_runs.workspace_id AND j.project_id=composition_runs.project_id));
