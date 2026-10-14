-- Preserve immutable audit records when a workspace removes its binding override.
ALTER TABLE workflow_prompt_binding_history DROP CONSTRAINT workflow_prompt_binding_history_binding_id_fkey;
CREATE FUNCTION protect_prompt_catalog_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id THEN RAISE EXCEPTION 'prompt catalog identity scope is immutable'; END IF;
 IF TG_TABLE_NAME='prompts' THEN
  IF NEW.node_key IS DISTINCT FROM OLD.node_key THEN RAISE EXCEPTION 'prompt catalog node identity is immutable'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER prompt_identity_stable BEFORE UPDATE ON prompts FOR EACH ROW EXECUTE FUNCTION protect_prompt_catalog_identity();
CREATE TRIGGER prompt_pack_identity_stable BEFORE UPDATE ON prompt_packs FOR EACH ROW EXECUTE FUNCTION protect_prompt_catalog_identity();
