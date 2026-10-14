-- Forward-only cutover. Source tables remain available for backup/retirement review.
CREATE TABLE workflow_states (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    workflow_id text NOT NULL,
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_job_id text NOT NULL REFERENCES runtime_jobs(id),
    fencing_token bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id,project_id,workflow_id)
);
CREATE TABLE workflow_records (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    workflow_id text NOT NULL,
    category text NOT NULL CHECK(category IN ('document','artifact','evaluation','audit','memory_report','execution_event')),
    record_kind text NOT NULL,
    record_id text NOT NULL,
    scope_key text NOT NULL DEFAULT '',
    version_number integer NOT NULL,
    content_hash text NOT NULL,
    payload jsonb NOT NULL,
    source_job_id text NOT NULL REFERENCES runtime_jobs(id),
    fencing_token bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(workspace_id,project_id,workflow_id,record_kind,record_id),
    UNIQUE(workspace_id,project_id,workflow_id,record_kind,scope_key,version_number)
);
CREATE TABLE workflow_checkpoints (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    workflow_id text NOT NULL,
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL,
    checkpoint_id text NOT NULL,
    parent_checkpoint_id text,
    checkpoint_type text NOT NULL,
    checkpoint bytea NOT NULL,
    metadata_type text NOT NULL,
    metadata bytea NOT NULL,
    source_job_id text NOT NULL REFERENCES runtime_jobs(id),
    fencing_token bigint NOT NULL,
    PRIMARY KEY(workspace_id,project_id,workflow_id,thread_id,checkpoint_ns,checkpoint_id)
);
CREATE TABLE workflow_checkpoint_writes (
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    workflow_id text NOT NULL,
    thread_id text NOT NULL,
    checkpoint_ns text NOT NULL,
    checkpoint_id text NOT NULL,
    task_id text NOT NULL,
    write_index integer NOT NULL,
    channel text NOT NULL,
    value_type text NOT NULL,
    value bytea NOT NULL,
    task_path text NOT NULL DEFAULT '',
    source_job_id text NOT NULL REFERENCES runtime_jobs(id),
    fencing_token bigint NOT NULL,
    PRIMARY KEY(workspace_id,project_id,workflow_id,thread_id,checkpoint_ns,checkpoint_id,task_id,write_index)
);
ALTER TABLE memory_nodes ADD COLUMN workflow_id text NOT NULL DEFAULT '';
ALTER TABLE memory_nodes ADD COLUMN source_job_id text REFERENCES runtime_jobs(id);
ALTER TABLE memory_nodes ADD COLUMN source_version_id text;
ALTER TABLE memory_nodes ADD COLUMN fencing_token bigint;
ALTER TABLE memory_nodes ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
DO $$ DECLARE constraint_name text; BEGIN
    SELECT conname INTO STRICT constraint_name FROM pg_constraint
      WHERE conrelid='memory_nodes'::regclass AND contype='u';
    EXECUTE format('ALTER TABLE memory_nodes DROP CONSTRAINT %I',constraint_name);
END $$;
ALTER TABLE memory_nodes ADD CONSTRAINT memory_nodes_source_scope
    UNIQUE(workspace_id,project_id,workflow_id,memory_type,source_id,source_version);

CREATE TABLE runtime_state_backfills (
    source_table text PRIMARY KEY,
    target_table text NOT NULL,
    source_count bigint NOT NULL,
    target_count bigint NOT NULL,
    source_hash text NOT NULL,
    target_hash text NOT NULL,
    verified_at timestamptz NOT NULL DEFAULT now(),
    CHECK(source_count=target_count AND source_hash=target_hash)
);

-- Only the table owner can bypass FORCE RLS, inside this migration transaction.
-- Restoring FORCE before commit preserves the application's tenant boundary.
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['novel_projects','novel_records','novel_checkpoints','novel_checkpoint_writes',
                             'novel_memories','memory_nodes','memory_embeddings'] LOOP
        EXECUTE format('ALTER TABLE %I NO FORCE ROW LEVEL SECURITY',t);
    END LOOP;
END $$;

INSERT INTO workflow_states SELECT workspace_id,project_id,'novel.write@1',state,source_job_id,fencing_token,updated_at
    FROM novel_projects;
INSERT INTO workflow_records
    SELECT workspace_id,project_id,'novel.write@1',
        CASE WHEN kind IN ('chapters','chapter_versions') THEN 'document'
             WHEN kind IN ('evaluations','benchmarks') THEN 'evaluation'
             WHEN kind='book_audits' THEN 'audit'
             WHEN kind IN ('memory_snapshots','memory_quality') THEN 'memory_report'
             WHEN kind IN ('planning_versions','candidates') THEN 'artifact' ELSE 'execution_event' END,
        kind,record_id,chapter_number::text,version_number,content_hash,payload,source_job_id,fencing_token,created_at
    FROM novel_records;
INSERT INTO workflow_checkpoints
    SELECT workspace_id,project_id,'novel.write@1',thread_id,checkpoint_ns,checkpoint_id,parent_checkpoint_id,
           checkpoint_type,checkpoint,metadata_type,metadata,source_job_id,fencing_token FROM novel_checkpoints;
INSERT INTO workflow_checkpoint_writes
    SELECT workspace_id,project_id,'novel.write@1',thread_id,checkpoint_ns,checkpoint_id,task_id,write_index,
           channel,value_type,value,task_path,source_job_id,fencing_token FROM novel_checkpoint_writes;
INSERT INTO memory_nodes(workspace_id,project_id,workflow_id,memory_type,source_id,source_version,content,
                         metadata,content_hash,source_job_id,source_version_id,fencing_token,updated_at)
    SELECT workspace_id,project_id,'novel.write@1',COALESCE(metadata->>'type','summary'),memory_id,1,content,
           metadata,encode(digest(content,'sha256'),'hex'),source_job_id,source_version_id,fencing_token,updated_at
    FROM novel_memories;
INSERT INTO memory_embeddings(id,project_id,embedding_model,index_version,embedding)
    SELECT n.id,n.project_id,COALESCE(m.metadata->>'embedding_model','unknown'),
           COALESCE(m.metadata->>'index_version','unknown'),m.embedding
    FROM novel_memories m JOIN memory_nodes n
      ON n.workspace_id=m.workspace_id AND n.project_id=m.project_id AND n.source_id=m.memory_id
     AND n.workflow_id='novel.write@1' AND n.source_version=1;

-- Hash identical source projections, including binary checkpoint data and vectors.
DO $$ DECLARE item record; source_count bigint; target_count bigint; source_hash text; target_hash text;
BEGIN
    FOR item IN SELECT * FROM (VALUES
      ('novel_projects','workflow_states',
       'SELECT to_jsonb(s) AS data FROM novel_projects s',
       'SELECT to_jsonb(t)-''workflow_id'' AS data FROM workflow_states t WHERE workflow_id=''novel.write@1'''),
      ('novel_records','workflow_records',
       'SELECT to_jsonb(s) AS data FROM novel_records s',
       'SELECT (to_jsonb(t)-''workflow_id''-''category''-''record_kind''-''scope_key'') || jsonb_build_object(''kind'',record_kind,''chapter_number'',scope_key::integer) AS data FROM workflow_records t WHERE workflow_id=''novel.write@1'''),
      ('novel_checkpoints','workflow_checkpoints',
       'SELECT to_jsonb(s) AS data FROM novel_checkpoints s',
       'SELECT to_jsonb(t)-''workflow_id'' AS data FROM workflow_checkpoints t WHERE workflow_id=''novel.write@1'''),
      ('novel_checkpoint_writes','workflow_checkpoint_writes',
       'SELECT to_jsonb(s) AS data FROM novel_checkpoint_writes s',
       'SELECT to_jsonb(t)-''workflow_id'' AS data FROM workflow_checkpoint_writes t WHERE workflow_id=''novel.write@1'''),
      ('novel_memories','memory_nodes/memory_embeddings',
       'SELECT to_jsonb(s) AS data FROM novel_memories s',
       'SELECT jsonb_build_object(''workspace_id'',n.workspace_id,''project_id'',n.project_id,''memory_id'',n.source_id,''content'',n.content,''metadata'',n.metadata,''embedding'',e.embedding::text,''source_job_id'',n.source_job_id,''source_version_id'',n.source_version_id,''fencing_token'',n.fencing_token,''updated_at'',n.updated_at) AS data FROM memory_nodes n JOIN memory_embeddings e ON e.id=n.id WHERE n.workflow_id=''novel.write@1''')
    ) AS mappings(source_table,target_table,source_query,target_query) LOOP
        EXECUTE 'SELECT count(*),encode(digest(COALESCE(string_agg(data::text,E''\n'' ORDER BY data::text),''''),''sha256''),''hex'') FROM ('||item.source_query||') q'
            INTO source_count,source_hash;
        EXECUTE 'SELECT count(*),encode(digest(COALESCE(string_agg(data::text,E''\n'' ORDER BY data::text),''''),''sha256''),''hex'') FROM ('||item.target_query||') q'
            INTO target_count,target_hash;
        INSERT INTO runtime_state_backfills(source_table,target_table,source_count,target_count,source_hash,target_hash)
            VALUES(item.source_table,item.target_table,source_count,target_count,source_hash,target_hash);
    END LOOP;
END $$;

DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['workflow_states','workflow_records','workflow_checkpoints','workflow_checkpoint_writes'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('CREATE POLICY %I ON %I USING(workspace_id=current_setting(''app.workspace_id'',true)) WITH CHECK(workspace_id=current_setting(''app.workspace_id'',true))',t||'_scope',t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['novel_projects','novel_records','novel_checkpoints','novel_checkpoint_writes',
                             'novel_memories','memory_nodes','memory_embeddings'] LOOP
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['novel_projects','novel_records','novel_checkpoints','novel_checkpoint_writes','novel_memories'] LOOP
        EXECUTE format('COMMENT ON TABLE %I IS %L',t,'Retired source after 013 generic cutover. Retain until backup restore and backfill verification; no application writes.');
    END LOOP;
END $$;
