-- Apply only after the operator has taken and restore-tested an external backup.
-- Archive/backfill guards run in the same transaction as table retirement.
DO $$ DECLARE t text; row_count bigint; row_hash text; proof runtime_resource_archive_checks%ROWTYPE;
BEGIN
    IF NOT EXISTS(SELECT 1 FROM platform_migrations
                  WHERE component='runtime' AND migration_name='013_generic_workflow_state')
       OR NOT EXISTS(SELECT 1 FROM platform_migrations
                  WHERE component='runtime' AND migration_name='015_legacy_resource_archive') THEN
        RAISE EXCEPTION 'generic backfill and archive migrations must be recorded before retirement';
    END IF;
    FOREACH t IN ARRAY ARRAY['novel_projects','novel_records','novel_checkpoints','novel_checkpoint_writes','novel_memories'] LOOP
        IF NOT EXISTS(SELECT 1 FROM runtime_state_backfills WHERE source_table=t
                      AND source_count=target_count AND source_hash=target_hash) THEN
            RAISE EXCEPTION 'generic backfill is unverified for %',t;
        END IF;
        SELECT * INTO STRICT proof FROM runtime_resource_archive_checks WHERE source_table=t;
        EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);
        EXECUTE format('SELECT count(*),encode(digest(COALESCE(string_agg(to_jsonb(s)::text,E''\n'' ORDER BY to_jsonb(s)::text),''''),''sha256''),''hex'') FROM %I s',t)
            INTO row_count,row_hash;
        IF row_count<>proof.source_count OR row_hash<>proof.source_hash THEN
            RAISE EXCEPTION 'source changed after archive: %',t;
        END IF;
    END LOOP;
END $$;
DROP TABLE novel_checkpoint_writes;
DROP TABLE novel_checkpoints;
DROP TABLE novel_memories;
DROP TABLE novel_records;
DROP TABLE novel_projects;
