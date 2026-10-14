-- Retirement is a separate forward step, gated by the prior count/hash checks.
DO $$
DECLARE item text; source_count bigint; archive_count bigint;
BEGIN
 IF (SELECT count(*) FROM resource_migration_validation v WHERE v.source_count=v.target_count)<>5 THEN
  RAISE EXCEPTION 'legacy resources have not been validated';
 END IF;
 FOREACH item IN ARRAY ARRAY['novels','novel_brief_versions','novel_records','novel_request_keys','novel_transfers'] LOOP
  EXECUTE format('SELECT count(*) FROM %I',item) INTO source_count;
  SELECT count(*) INTO archive_count FROM legacy_resource_archive WHERE source_table=item;
  IF source_count<>archive_count THEN RAISE EXCEPTION 'resource source changed since archival: %',item; END IF;
 END LOOP;
END $$;
DROP INDEX jobs_single_active_novel_idx;
CREATE UNIQUE INDEX jobs_single_active_project_idx ON jobs(project_id)
 WHERE status IN ('queued','running','cancel_requested');
DROP TABLE novel_transfers,novel_request_keys,novel_records,novel_brief_versions,novels;
