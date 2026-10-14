CREATE TABLE creation_policy_sets (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid REFERENCES workspaces(id),
 key text NOT NULL CHECK(key<>''),
 name text NOT NULL CHECK(name<>''),
 current_version integer NOT NULL DEFAULT 0 CHECK(current_version>=0),
 UNIQUE NULLS NOT DISTINCT(workspace_id,key)
);
CREATE TABLE creation_policy_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 policy_set_id uuid NOT NULL REFERENCES creation_policy_sets(id),
 workspace_id uuid REFERENCES workspaces(id),
 version integer NOT NULL CHECK(version>0),
 status text NOT NULL CHECK(status IN ('draft','published','disabled')),
 payload jsonb NOT NULL CHECK(jsonb_typeof(payload)='object'),
 content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
 created_by uuid REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(policy_set_id,version)
);
CREATE TABLE workflow_creation_policy_bindings (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid REFERENCES workspaces(id),
 workflow text NOT NULL CHECK(workflow IN ('novel.write@1','content.generate@1','content.adapt@1','memory.refresh@1')),
 policy_version_id uuid NOT NULL REFERENCES creation_policy_versions(id),
 created_by uuid REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE NULLS NOT DISTINCT(workspace_id,workflow)
);
CREATE TABLE workflow_creation_policy_binding_history (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 binding_id uuid NOT NULL,
 workspace_id uuid REFERENCES workspaces(id),
 workflow text NOT NULL,
 policy_version_id uuid NOT NULL REFERENCES creation_policy_versions(id),
 created_by uuid REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER creation_policy_versions_immutable BEFORE UPDATE OR DELETE ON creation_policy_versions FOR EACH ROW EXECUTE FUNCTION reject_prompt_catalog_mutation();
CREATE TRIGGER creation_policy_history_immutable BEFORE UPDATE OR DELETE ON workflow_creation_policy_binding_history FOR EACH ROW EXECUTE FUNCTION reject_prompt_catalog_mutation();
CREATE FUNCTION protect_creation_policy_scope() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE parent_scope uuid; parent_status text;
BEGIN
 IF TG_TABLE_NAME='creation_policy_sets' THEN
  IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id THEN RAISE EXCEPTION 'creation policy identity scope is immutable'; END IF;
 ELSIF TG_TABLE_NAME='creation_policy_versions' THEN
  SELECT workspace_id INTO parent_scope FROM creation_policy_sets WHERE id=NEW.policy_set_id;
  IF parent_scope IS DISTINCT FROM NEW.workspace_id THEN RAISE EXCEPTION 'creation policy version scope mismatch'; END IF;
 ELSE
  IF TG_OP='UPDATE' AND (NEW.workspace_id IS DISTINCT FROM OLD.workspace_id OR NEW.workflow IS DISTINCT FROM OLD.workflow) THEN RAISE EXCEPTION 'creation policy binding identity is immutable'; END IF;
  SELECT workspace_id,status INTO parent_scope,parent_status FROM creation_policy_versions WHERE id=NEW.policy_version_id;
  IF (parent_scope IS NOT NULL AND parent_scope IS DISTINCT FROM NEW.workspace_id) OR parent_status IS DISTINCT FROM 'published' THEN RAISE EXCEPTION 'workflow requires published creation policy in scope'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER creation_policy_identity_scope BEFORE UPDATE ON creation_policy_sets FOR EACH ROW EXECUTE FUNCTION protect_creation_policy_scope();
CREATE TRIGGER creation_policy_version_scope BEFORE INSERT ON creation_policy_versions FOR EACH ROW EXECUTE FUNCTION protect_creation_policy_scope();
CREATE TRIGGER creation_policy_binding_scope BEFORE INSERT OR UPDATE ON workflow_creation_policy_bindings FOR EACH ROW EXECUTE FUNCTION protect_creation_policy_scope();
CREATE FUNCTION record_creation_policy_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 INSERT INTO workflow_creation_policy_binding_history(binding_id,workspace_id,workflow,policy_version_id,created_by) VALUES(NEW.id,NEW.workspace_id,NEW.workflow,NEW.policy_version_id,NEW.created_by);
 RETURN NEW;
END $$;
CREATE TRIGGER creation_policy_binding_audit AFTER INSERT OR UPDATE ON workflow_creation_policy_bindings FOR EACH ROW EXECUTE FUNCTION record_creation_policy_binding();
DO $$ DECLARE tab text; BEGIN
 FOREACH tab IN ARRAY ARRAY['creation_policy_sets','creation_policy_versions','workflow_creation_policy_bindings','workflow_creation_policy_binding_history'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',tab);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',tab);
  EXECUTE format('CREATE POLICY %I ON %I FOR SELECT USING(workspace_id IS NULL OR workspace_id::text=current_setting(''app.workspace_id'',true))',tab||'_read',tab);
  EXECUTE format('CREATE POLICY %I ON %I FOR ALL USING(workspace_id::text=current_setting(''app.workspace_id'',true)) WITH CHECK(workspace_id::text=current_setting(''app.workspace_id'',true))',tab||'_write',tab);
 END LOOP;
END $$;
