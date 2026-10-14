-- Copy legacy state into generic authority and validate before retirement.
ALTER TABLE projects ADD COLUMN content_type text;
UPDATE projects SET content_type=project_type;
ALTER TABLE projects ALTER COLUMN content_type SET NOT NULL;
ALTER TABLE projects DROP CONSTRAINT projects_project_type_check;
CREATE FUNCTION default_project_content_type() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.content_type IS NULL THEN NEW.content_type=NEW.project_type; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER project_content_type_default BEFORE INSERT ON projects FOR EACH ROW EXECUTE FUNCTION default_project_content_type();

CREATE TABLE project_projections (
 id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
 input jsonb NOT NULL,
 state jsonb NOT NULL DEFAULT '{}',
 state_hash text NOT NULL DEFAULT '',
 brief_version integer NOT NULL DEFAULT 1 CHECK(brief_version>0),
 revision bigint NOT NULL DEFAULT 1,
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO project_projections
SELECT n.id,n.workspace_id,n.document_id,
 jsonb_build_object('title',p.name,'content_type',p.content_type,'input',n.input-'creative_brief','brief',COALESCE(n.input->'creative_brief','{}')),
 n.state,n.state_hash,n.brief_version,n.revision,n.created_at,n.updated_at
FROM novels n JOIN projects p ON p.id=n.id;

CREATE TABLE project_records (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 kind text NOT NULL,
 record_key text NOT NULL,
 block_index integer NOT NULL DEFAULT 0 CHECK(block_index>=0),
 version_number integer NOT NULL DEFAULT 0 CHECK(version_number>=0),
 artifact_type text NOT NULL DEFAULT '',
 payload jsonb NOT NULL,
 content_hash text NOT NULL,
 stale boolean NOT NULL DEFAULT false,
 baseline boolean NOT NULL DEFAULT false,
 source_job_id uuid REFERENCES jobs(id) ON DELETE SET NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(project_id,kind,record_key)
);
INSERT INTO project_records
SELECT id,workspace_id,novel_id,CASE kind WHEN 'book_audit' THEN 'audit' ELSE kind END,
 record_key,chapter_number,version_number,artifact_type,payload,content_hash,stale,baseline,source_job_id,created_at
FROM novel_records WHERE kind<>'chapter';
CREATE INDEX project_records_query_idx ON project_records(project_id,kind,block_index,version_number DESC);
CREATE UNIQUE INDEX project_evaluation_baseline_idx ON project_records(project_id,block_index) WHERE kind='evaluation' AND baseline;

-- Briefs and formal block history are ordinary immutable Content IR versions.
INSERT INTO documents(id,workspace_id,project_id,document_type,title)
SELECT gen_random_uuid(),workspace_id,id,'brief','Project brief' FROM novels;
INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,content_hash,payload,status,created_at)
SELECT gen_random_uuid(),b.workspace_id,d.id,b.version,'1.0',
 encode(digest(convert_to(jsonb_build_object('schema_version','1.0','blocks','[]'::jsonb,'brief',b.payload,'change_summary',b.change_summary)::text,'UTF8'),'sha256'),'hex'),
 jsonb_build_object('schema_version','1.0','blocks','[]'::jsonb,'brief',b.payload,'change_summary',b.change_summary),'draft',b.created_at
FROM novel_brief_versions b JOIN documents d ON d.project_id=b.novel_id AND d.document_type='brief';

INSERT INTO documents(id,workspace_id,project_id,document_type,title)
SELECT gen_random_uuid(),workspace_id,novel_id,'block','Block '||chapter_number
FROM novel_records WHERE kind='chapter' GROUP BY workspace_id,novel_id,chapter_number;
INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,content_hash,payload,status,created_at)
SELECT r.id,r.workspace_id,d.id,r.version_number,'1.0',
 encode(digest(convert_to(jsonb_build_object('schema_version','1.0','blocks',jsonb_build_array(jsonb_build_object('type','text','text',COALESCE(r.payload->>'content',''),'position',0)),'record',r.payload,'record_key',r.record_key,'record_hash',r.content_hash,'block_index',r.chapter_number,'source_job_id',r.source_job_id)::text,'UTF8'),'sha256'),'hex'),
 jsonb_build_object('schema_version','1.0','blocks',jsonb_build_array(jsonb_build_object('type','text','text',COALESCE(r.payload->>'content',''),'position',0)),'record',r.payload,'record_key',r.record_key,'record_hash',r.content_hash,'block_index',r.chapter_number,'source_job_id',r.source_job_id),'draft',r.created_at
FROM novel_records r JOIN documents d ON d.project_id=r.novel_id AND d.document_type='block' AND d.title='Block '||r.chapter_number
WHERE r.kind='chapter';
INSERT INTO content_blocks(workspace_id,document_version_id,block_index,block_type,payload)
SELECT v.workspace_id,v.id,0,'text',v.payload->'blocks'->0 FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE d.document_type='block';
UPDATE documents d SET current_version_id=(SELECT id FROM document_versions v WHERE v.document_id=d.id ORDER BY version DESC LIMIT 1) WHERE d.document_type IN ('brief','block');

CREATE VIEW project_content_versions WITH (security_invoker=true) AS
SELECT v.id,v.workspace_id,d.project_id,'content'::text AS kind,v.payload->>'record_key' AS record_key,
 (v.payload->>'block_index')::integer AS block_index,v.version AS version_number,''::text AS artifact_type,
 v.payload->'record' AS payload,v.payload->>'record_hash' AS content_hash,false AS stale,false AS baseline,
 NULLIF(v.payload->>'source_job_id','')::uuid AS source_job_id,v.created_at
FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE d.document_type='block';

CREATE VIEW project_brief_versions WITH (security_invoker=true) AS
SELECT v.workspace_id,d.project_id,v.version,v.payload->'brief' AS payload,
 COALESCE(v.payload->>'change_summary','') AS change_summary,v.created_at
FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE d.document_type='brief';
CREATE FUNCTION write_project_brief_version() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE doc uuid; version_id uuid; body jsonb;
BEGIN
 SELECT id INTO doc FROM documents WHERE workspace_id=NEW.workspace_id AND project_id=NEW.project_id AND document_type='brief' FOR UPDATE;
 IF doc IS NULL THEN
  INSERT INTO documents(workspace_id,project_id,document_type,title) VALUES(NEW.workspace_id,NEW.project_id,'brief','Project brief') RETURNING id INTO doc;
 END IF;
 body=jsonb_build_object('schema_version','1.0','blocks','[]'::jsonb,'brief',NEW.payload,'change_summary',COALESCE(NEW.change_summary,''));
 IF TG_OP='UPDATE' THEN
  UPDATE document_versions SET payload=body,content_hash=encode(digest(convert_to(body::text,'UTF8'),'sha256'),'hex') WHERE document_id=doc AND version=NEW.version RETURNING id INTO version_id;
 ELSE
  INSERT INTO document_versions(workspace_id,document_id,version,ir_schema_version,content_hash,payload,status) VALUES(NEW.workspace_id,doc,NEW.version,'1.0',encode(digest(convert_to(body::text,'UTF8'),'sha256'),'hex'),body,'draft') RETURNING id INTO version_id;
 END IF;
 UPDATE documents SET current_version_id=version_id,updated_at=now() WHERE id=doc;
 RETURN NEW;
END $$;
CREATE TRIGGER project_brief_versions_write INSTEAD OF INSERT OR UPDATE ON project_brief_versions FOR EACH ROW EXECUTE FUNCTION write_project_brief_version();

INSERT INTO idempotency_keys(workspace_id,operation,idempotency_key,payload_hash,status_code,response,created_at)
SELECT workspace_id,'project.resource',key,decode(fingerprint,'hex'),201,jsonb_build_object('project_id',novel_id),created_at FROM novel_request_keys;
CREATE VIEW project_request_keys WITH (security_invoker=true) AS SELECT workspace_id,idempotency_key AS key,encode(payload_hash,'hex') AS fingerprint,(response->>'project_id')::uuid AS project_id,created_at FROM idempotency_keys WHERE operation='project.resource';
CREATE FUNCTION write_project_request_key() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO idempotency_keys(workspace_id,operation,idempotency_key,payload_hash,status_code,response)
 VALUES(NEW.workspace_id,'project.resource',NEW.key,decode(NEW.fingerprint,'hex'),201,jsonb_build_object('project_id',NEW.project_id));
 RETURN NEW;
END $$;
CREATE TRIGGER project_request_keys_write INSTEAD OF INSERT ON project_request_keys FOR EACH ROW EXECUTE FUNCTION write_project_request_key();

CREATE TABLE project_transfers (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 status text NOT NULL CHECK(status IN ('queued','running','completed','cancelled','failed')),
 format text NOT NULL CHECK(format IN ('backup','markdown','txt','docx','epub')),
 result bytea,content_hash text,error text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO project_transfers SELECT * FROM novel_transfers;

CREATE TABLE resource_migration_validation (
 source_table text PRIMARY KEY,source_count bigint NOT NULL,target_count bigint NOT NULL,validated_at timestamptz NOT NULL DEFAULT now()
);
DO $$
DECLARE source_count bigint; target_count bigint; item text;
BEGIN
 SELECT count(*) INTO source_count FROM novels;
 SELECT count(*) INTO target_count FROM project_projections;
 IF source_count<>target_count OR EXISTS(SELECT 1 FROM novels n JOIN project_projections p ON p.id=n.id WHERE n.state<>p.state OR n.state_hash<>p.state_hash OR n.input<>((p.input->'input')||jsonb_build_object('creative_brief',p.input->'brief'))) THEN RAISE EXCEPTION 'project backfill validation failed'; END IF;
 INSERT INTO resource_migration_validation VALUES('novels',source_count,target_count,now());
 SELECT count(*) INTO source_count FROM novel_records;
 SELECT (SELECT count(*) FROM project_records)+(SELECT count(*) FROM project_content_versions) INTO target_count;
 IF source_count<>target_count OR EXISTS(SELECT 1 FROM novel_records n LEFT JOIN (SELECT id,payload,content_hash FROM project_records UNION ALL SELECT id,payload,content_hash FROM project_content_versions) p ON p.id=n.id WHERE p.id IS NULL OR p.payload<>n.payload OR p.content_hash<>n.content_hash) THEN RAISE EXCEPTION 'record backfill validation failed'; END IF;
 INSERT INTO resource_migration_validation VALUES('novel_records',source_count,target_count,now());
 SELECT count(*) INTO source_count FROM novel_brief_versions;
 SELECT count(*) INTO target_count FROM project_brief_versions;
 IF source_count<>target_count OR EXISTS(SELECT 1 FROM novel_brief_versions n LEFT JOIN project_brief_versions p ON p.project_id=n.novel_id AND p.version=n.version WHERE p.project_id IS NULL OR p.payload<>n.payload) THEN RAISE EXCEPTION 'brief backfill validation failed'; END IF;
 INSERT INTO resource_migration_validation VALUES('novel_brief_versions',source_count,target_count,now());
 SELECT count(*) INTO source_count FROM novel_request_keys;
 SELECT count(*) INTO target_count FROM project_request_keys;
 IF source_count<>target_count THEN RAISE EXCEPTION 'idempotency backfill validation failed'; END IF;
 INSERT INTO resource_migration_validation VALUES('novel_request_keys',source_count,target_count,now());
 SELECT count(*) INTO source_count FROM novel_transfers;
 SELECT count(*) INTO target_count FROM project_transfers;
 IF source_count<>target_count OR EXISTS(SELECT 1 FROM novel_transfers n JOIN project_transfers p ON p.id=n.id WHERE n.result IS DISTINCT FROM p.result OR n.content_hash IS DISTINCT FROM p.content_hash) THEN RAISE EXCEPTION 'transfer backfill validation failed'; END IF;
 INSERT INTO resource_migration_validation VALUES('novel_transfers',source_count,target_count,now());
 FOREACH item IN ARRAY ARRAY['project_projections','project_records','project_transfers'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',item);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',item);
  EXECUTE format('CREATE POLICY %I ON %I USING (workspace_id::text=current_setting(''app.workspace_id'',true)) WITH CHECK (workspace_id::text=current_setting(''app.workspace_id'',true))',item||'_workspace_policy',item);
 END LOOP;
END $$;
