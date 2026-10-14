-- Prompt identities are mutable; executable versions and pack membership are immutable.
CREATE TABLE prompt_node_contracts (
 node_key text PRIMARY KEY,
 variables_schema jsonb NOT NULL CHECK(jsonb_typeof(variables_schema)='object'),
 output_schema jsonb NOT NULL CHECK(jsonb_typeof(output_schema)='object'),
 renderer_version text NOT NULL CHECK(renderer_version='literal-v1')
);
CREATE TABLE prompts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid REFERENCES workspaces(id),
 key text NOT NULL CHECK(key<>''),
 node_key text NOT NULL REFERENCES prompt_node_contracts(node_key),
 name text NOT NULL,
 description text NOT NULL DEFAULT '',
 current_version integer NOT NULL DEFAULT 0 CHECK(current_version>=0),
 UNIQUE NULLS NOT DISTINCT(workspace_id,key)
);
CREATE TABLE prompt_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 prompt_id uuid NOT NULL REFERENCES prompts(id),
 workspace_id uuid REFERENCES workspaces(id),
 version integer NOT NULL CHECK(version>0),
 messages jsonb NOT NULL CHECK(jsonb_typeof(messages)='array'),
 variables_schema jsonb NOT NULL CHECK(jsonb_typeof(variables_schema)='object'),
 output_schema jsonb NOT NULL CHECK(jsonb_typeof(output_schema)='object'),
 renderer_version text NOT NULL CHECK(renderer_version='literal-v1'),
 status text NOT NULL CHECK(status IN ('draft','published','disabled')),
 content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
 created_by uuid REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(prompt_id,version)
);
CREATE TABLE prompt_packs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid REFERENCES workspaces(id),
 key text NOT NULL CHECK(key<>''),
 name text NOT NULL,
 description text NOT NULL DEFAULT '',
 current_version integer NOT NULL DEFAULT 0 CHECK(current_version>=0),
 UNIQUE NULLS NOT DISTINCT(workspace_id,key)
);
CREATE TABLE prompt_pack_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 pack_id uuid NOT NULL REFERENCES prompt_packs(id),
 workspace_id uuid REFERENCES workspaces(id),
 version integer NOT NULL CHECK(version>0),
 status text NOT NULL CHECK(status IN ('draft','published','disabled')),
 content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
 created_by uuid REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 created_xid xid8 NOT NULL DEFAULT pg_current_xact_id(),
 UNIQUE(pack_id,version)
);
CREATE TABLE prompt_pack_entries (
 pack_version_id uuid NOT NULL REFERENCES prompt_pack_versions(id),
 node_key text NOT NULL REFERENCES prompt_node_contracts(node_key),
 prompt_version_id uuid NOT NULL REFERENCES prompt_versions(id),
 PRIMARY KEY(pack_version_id,node_key)
);
CREATE TABLE workflow_prompt_bindings (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid REFERENCES workspaces(id),
 workflow text NOT NULL CHECK(workflow IN ('novel.write@1','content.generate@1','content.adapt@1','memory.refresh@1')),
 pack_version_id uuid NOT NULL REFERENCES prompt_pack_versions(id),
 created_by uuid REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE NULLS NOT DISTINCT(workspace_id,workflow)
);
CREATE TABLE workflow_prompt_binding_history (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 binding_id uuid NOT NULL REFERENCES workflow_prompt_bindings(id),
 workspace_id uuid REFERENCES workspaces(id),
 workflow text NOT NULL,
 pack_version_id uuid NOT NULL REFERENCES prompt_pack_versions(id),
 created_by uuid REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE FUNCTION reject_prompt_catalog_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'prompt catalog versions are immutable'; END $$;
CREATE TRIGGER prompt_versions_immutable BEFORE UPDATE OR DELETE ON prompt_versions FOR EACH ROW EXECUTE FUNCTION reject_prompt_catalog_mutation();
CREATE TRIGGER prompt_pack_versions_immutable BEFORE UPDATE OR DELETE ON prompt_pack_versions FOR EACH ROW EXECUTE FUNCTION reject_prompt_catalog_mutation();
CREATE TRIGGER prompt_pack_entries_immutable BEFORE UPDATE OR DELETE ON prompt_pack_entries FOR EACH ROW EXECUTE FUNCTION reject_prompt_catalog_mutation();
CREATE TRIGGER prompt_binding_history_immutable BEFORE UPDATE OR DELETE ON workflow_prompt_binding_history FOR EACH ROW EXECUTE FUNCTION reject_prompt_catalog_mutation();
CREATE FUNCTION validate_prompt_catalog_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent_scope uuid;
BEGIN
 IF TG_TABLE_NAME='prompt_versions' THEN
  SELECT workspace_id INTO parent_scope FROM prompts WHERE id=NEW.prompt_id;
 ELSE
  SELECT workspace_id INTO parent_scope FROM prompt_packs WHERE id=NEW.pack_id;
 END IF;
 IF parent_scope IS DISTINCT FROM NEW.workspace_id THEN RAISE EXCEPTION 'prompt version scope mismatch'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER prompt_version_scope BEFORE INSERT ON prompt_versions FOR EACH ROW EXECUTE FUNCTION validate_prompt_catalog_scope();
CREATE TRIGGER prompt_pack_version_scope BEFORE INSERT ON prompt_pack_versions FOR EACH ROW EXECUTE FUNCTION validate_prompt_catalog_scope();
CREATE FUNCTION validate_prompt_pack_entry() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE pack_scope uuid; prompt_scope uuid; actual_node text; creation_xid xid8; prompt_status text;
BEGIN
 SELECT workspace_id,created_xid INTO pack_scope,creation_xid FROM prompt_pack_versions WHERE id=NEW.pack_version_id;
 IF creation_xid IS DISTINCT FROM pg_current_xact_id() THEN RAISE EXCEPTION 'prompt pack entries are sealed'; END IF;
 SELECT v.workspace_id,p.node_key,v.status INTO prompt_scope,actual_node,prompt_status FROM prompt_versions v JOIN prompts p ON p.id=v.prompt_id WHERE v.id=NEW.prompt_version_id;
 IF actual_node IS DISTINCT FROM NEW.node_key OR (prompt_scope IS NOT NULL AND prompt_scope IS DISTINCT FROM pack_scope) THEN RAISE EXCEPTION 'prompt pack entry scope or node mismatch'; END IF;
 IF prompt_status IS DISTINCT FROM 'published' THEN RAISE EXCEPTION 'prompt pack requires published prompt versions'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER prompt_pack_entry_scope BEFORE INSERT ON prompt_pack_entries FOR EACH ROW EXECUTE FUNCTION validate_prompt_pack_entry();
CREATE FUNCTION validate_workflow_prompt_binding() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE pack_scope uuid; pack_status text;
BEGIN
 SELECT workspace_id,status INTO pack_scope,pack_status FROM prompt_pack_versions WHERE id=NEW.pack_version_id;
 IF (pack_scope IS NOT NULL AND pack_scope IS DISTINCT FROM NEW.workspace_id) OR pack_status IS DISTINCT FROM 'published' THEN RAISE EXCEPTION 'workflow requires published prompt pack in scope'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER workflow_prompt_binding_scope BEFORE INSERT OR UPDATE ON workflow_prompt_bindings FOR EACH ROW EXECUTE FUNCTION validate_workflow_prompt_binding();
CREATE FUNCTION record_workflow_prompt_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO workflow_prompt_binding_history(binding_id,workspace_id,workflow,pack_version_id,created_by) VALUES(NEW.id,NEW.workspace_id,NEW.workflow,NEW.pack_version_id,NEW.created_by);
 RETURN NEW;
END $$;
CREATE TRIGGER workflow_prompt_binding_audit AFTER INSERT OR UPDATE ON workflow_prompt_bindings FOR EACH ROW EXECUTE FUNCTION record_workflow_prompt_binding();
DO $$ DECLARE tab text; BEGIN
 FOREACH tab IN ARRAY ARRAY['prompts','prompt_versions','prompt_packs','prompt_pack_versions','workflow_prompt_bindings','workflow_prompt_binding_history'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tab);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',tab);
  EXECUTE format('CREATE POLICY %I ON %I FOR SELECT USING(workspace_id IS NULL OR workspace_id::text=current_setting(''app.workspace_id'',true))',tab||'_read',tab);
  EXECUTE format('CREATE POLICY %I ON %I FOR ALL USING(workspace_id::text=current_setting(''app.workspace_id'',true)) WITH CHECK(workspace_id::text=current_setting(''app.workspace_id'',true))',tab||'_write',tab);
 END LOOP;
END $$;
ALTER TABLE prompt_pack_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_pack_entries FORCE ROW LEVEL SECURITY;
CREATE POLICY prompt_pack_entries_read ON prompt_pack_entries FOR SELECT USING(EXISTS(SELECT 1 FROM prompt_pack_versions v WHERE v.id=pack_version_id));
CREATE POLICY prompt_pack_entries_write ON prompt_pack_entries FOR INSERT WITH CHECK(EXISTS(SELECT 1 FROM prompt_pack_versions v WHERE v.id=pack_version_id AND v.workspace_id::text=current_setting('app.workspace_id',true)));
ALTER TABLE prompt_node_contracts ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_node_contracts FORCE ROW LEVEL SECURITY;
CREATE POLICY prompt_node_contracts_read ON prompt_node_contracts FOR SELECT USING(true);
