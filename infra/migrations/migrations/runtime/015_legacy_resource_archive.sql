-- Keep a tenant-scoped lossless archive before retiring the original tables.
CREATE TABLE legacy_runtime_resource_archive (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    source_table text NOT NULL,
    source_key text NOT NULL,
    payload jsonb NOT NULL,
    content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
    archived_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id,source_table,source_key)
);
CREATE TABLE runtime_resource_archive_checks (
    source_table text PRIMARY KEY,
    source_count bigint NOT NULL,
    archive_count bigint NOT NULL,
    source_hash text NOT NULL,
    archive_hash text NOT NULL,
    verified_at timestamptz NOT NULL DEFAULT now(),
    CHECK(source_count=archive_count AND source_hash=archive_hash)
);
DO $$ DECLARE t text; source_count bigint; archive_count bigint; source_hash text; archive_hash text;
BEGIN
    FOREACH t IN ARRAY ARRAY['novel_projects','novel_records','novel_checkpoints','novel_checkpoint_writes','novel_memories'] LOOP
        EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);
        EXECUTE format('INSERT INTO legacy_runtime_resource_archive(workspace_id,project_id,source_table,source_key,payload,content_hash)
                       SELECT workspace_id,project_id,%L,encode(digest(to_jsonb(s)::text,''sha256''),''hex''),
                              to_jsonb(s),encode(digest(to_jsonb(s)::text,''sha256''),''hex'') FROM %I s',t,t);
        EXECUTE format('SELECT count(*),encode(digest(COALESCE(string_agg(to_jsonb(s)::text,E''\n'' ORDER BY to_jsonb(s)::text),''''),''sha256''),''hex'') FROM %I s',t)
            INTO source_count,source_hash;
        SELECT count(*),encode(digest(COALESCE(string_agg(payload::text,E'\n' ORDER BY payload::text),''),'sha256'),'hex')
            INTO archive_count,archive_hash FROM legacy_runtime_resource_archive WHERE source_table=t;
        INSERT INTO runtime_resource_archive_checks VALUES(t,source_count,archive_count,source_hash,archive_hash,now());
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
    END LOOP;
END $$;
ALTER TABLE legacy_runtime_resource_archive ENABLE ROW LEVEL SECURITY;
ALTER TABLE legacy_runtime_resource_archive FORCE ROW LEVEL SECURITY;
CREATE POLICY legacy_runtime_resource_archive_scope ON legacy_runtime_resource_archive
    USING(workspace_id=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id=current_setting('app.workspace_id',true));
CREATE FUNCTION immutable_runtime_resource_archive() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'legacy Runtime archive is immutable'; END $$;
CREATE TRIGGER runtime_resource_archive_immutable BEFORE UPDATE OR DELETE ON legacy_runtime_resource_archive
    FOR EACH ROW EXECUTE FUNCTION immutable_runtime_resource_archive();
