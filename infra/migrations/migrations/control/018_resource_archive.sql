-- Preserve exact source rows independently of the live generic authority.
CREATE TABLE legacy_resource_archive (
 source_table text NOT NULL,
 source_key text NOT NULL,
 workspace_id uuid NOT NULL,
 payload jsonb NOT NULL,
 content_hash text NOT NULL,
 archived_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(source_table,source_key)
);
DO $$
DECLARE item text; source_count bigint; archive_count bigint;
BEGIN
 FOREACH item IN ARRAY ARRAY['novels','novel_brief_versions','novel_records','novel_request_keys','novel_transfers'] LOOP
  EXECUTE format('INSERT INTO legacy_resource_archive(source_table,source_key,workspace_id,payload,content_hash) SELECT %L,encode(digest(convert_to(to_jsonb(source)::text,''UTF8''),''sha256''),''hex''),workspace_id,to_jsonb(source),encode(digest(convert_to(to_jsonb(source)::text,''UTF8''),''sha256''),''hex'') FROM %I source',item,item);
  EXECUTE format('SELECT count(*) FROM %I',item) INTO source_count;
  SELECT count(*) INTO archive_count FROM legacy_resource_archive WHERE source_table=item;
  IF source_count<>archive_count THEN RAISE EXCEPTION 'resource archive count mismatch for %',item; END IF;
  EXECUTE format('SELECT count(*) FROM %I source WHERE NOT EXISTS(SELECT 1 FROM legacy_resource_archive archive WHERE archive.source_table=%L AND archive.content_hash=encode(digest(convert_to(to_jsonb(source)::text,''UTF8''),''sha256''),''hex'') AND archive.payload=to_jsonb(source))',item,item) INTO archive_count;
  IF archive_count<>0 THEN RAISE EXCEPTION 'resource archive hash mismatch for %',item; END IF;
 END LOOP;
END $$;
ALTER TABLE legacy_resource_archive ENABLE ROW LEVEL SECURITY;
ALTER TABLE legacy_resource_archive FORCE ROW LEVEL SECURITY;
CREATE POLICY legacy_resource_archive_scope ON legacy_resource_archive
 USING(workspace_id::text=current_setting('app.workspace_id',true))
 WITH CHECK(workspace_id::text=current_setting('app.workspace_id',true));
