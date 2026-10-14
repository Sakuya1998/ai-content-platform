CREATE TABLE knowledge_packs (
    id uuid PRIMARY KEY,
    workspace_id text NOT NULL,
    source_project_id text NOT NULL,
    name text NOT NULL,
    nodes jsonb NOT NULL,
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE knowledge_pack_bindings (
    workspace_id text NOT NULL,
    target_project_id text NOT NULL,
    pack_id uuid NOT NULL REFERENCES knowledge_packs(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id,target_project_id,pack_id)
);
CREATE TABLE runtime_maintenance_runs (
    id uuid PRIMARY KEY,
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    operation text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['knowledge_packs','knowledge_pack_bindings','runtime_maintenance_runs'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY %I ON %I USING(workspace_id=current_setting(''app.workspace_id'',true)) WITH CHECK(workspace_id=current_setting(''app.workspace_id'',true))',t||'_scope',t);
    END LOOP;
END $$;
CREATE FUNCTION immutable_knowledge_pack() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'published knowledge packs are immutable'; END $$;
CREATE TRIGGER knowledge_pack_immutable BEFORE UPDATE ON knowledge_packs
    FOR EACH ROW EXECUTE FUNCTION immutable_knowledge_pack();
