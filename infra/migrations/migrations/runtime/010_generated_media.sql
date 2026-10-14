-- Synchronous image/TTS providers cannot safely replay a request after a lost response.
CREATE TABLE generated_media_tasks (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    job_id text NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    modality text NOT NULL CHECK (modality IN ('image','audio')),
    request jsonb NOT NULL,
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    route_identity jsonb NOT NULL CHECK (NOT route_identity ? 'api_key'),
    review jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL CHECK (state IN ('prepared','submitting','submission_unknown','outputs_ready','failed','cancelled')),
    fencing_token bigint NOT NULL,
    outputs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(outputs)='array'),
    vendor_request_id text,
    usage jsonb NOT NULL DEFAULT '{}'::jsonb,
    error jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id,job_id),
    CHECK (state!='outputs_ready' OR jsonb_array_length(outputs)>0)
);
CREATE FUNCTION generated_media_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id OR NEW.project_id IS DISTINCT FROM OLD.project_id
        OR NEW.job_id IS DISTINCT FROM OLD.job_id OR NEW.modality IS DISTINCT FROM OLD.modality
        OR NEW.request IS DISTINCT FROM OLD.request OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
        OR NEW.route_identity IS DISTINCT FROM OLD.route_identity
        OR (jsonb_array_length(OLD.outputs)>0 AND (
            NEW.outputs IS DISTINCT FROM OLD.outputs OR NEW.usage IS DISTINCT FROM OLD.usage
            OR NEW.vendor_request_id IS DISTINCT FROM OLD.vendor_request_id)) THEN
        RAISE EXCEPTION 'generated media identity and archived outputs are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER generated_media_identity BEFORE UPDATE ON generated_media_tasks
    FOR EACH ROW EXECUTE FUNCTION generated_media_immutable();
ALTER TABLE generated_media_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE generated_media_tasks FORCE ROW LEVEL SECURITY;
CREATE POLICY generated_media_scope ON generated_media_tasks
    USING (workspace_id=current_setting('app.workspace_id',true)
        AND (NULLIF(current_setting('app.project_id',true),'') IS NULL
            OR project_id=current_setting('app.project_id',true))
        AND EXISTS(SELECT 1 FROM runtime_jobs j WHERE j.id=generated_media_tasks.job_id
            AND j.workspace_id=generated_media_tasks.workspace_id AND j.project_id=generated_media_tasks.project_id))
    WITH CHECK (workspace_id=current_setting('app.workspace_id',true)
        AND (NULLIF(current_setting('app.project_id',true),'') IS NULL
            OR project_id=current_setting('app.project_id',true))
        AND EXISTS(SELECT 1 FROM runtime_jobs j WHERE j.id=generated_media_tasks.job_id
            AND j.workspace_id=generated_media_tasks.workspace_id AND j.project_id=generated_media_tasks.project_id));
