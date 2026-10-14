-- Cookie sessions and workspace-scoped immutable governance versions.
ALTER TABLE users ADD COLUMN IF NOT EXISTS username text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name text NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled boolean NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS users_username_identity_idx ON users(lower(username)) WHERE username IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_email_identity_idx ON users(lower(email)) WHERE password_hash IS NOT NULL;
CREATE TABLE IF NOT EXISTS identity_sessions (
 token_hash text PRIMARY KEY,
 user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 expires_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS identity_sessions_user_idx ON identity_sessions(user_id);
CREATE TABLE IF NOT EXISTS governance_resources (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 kind text NOT NULL CHECK(kind IN ('content_types','styles','creative_templates','quality_policies','prompt_pack','skill','tool','mcp_policy')),
 key text NOT NULL,
 current_version integer NOT NULL DEFAULT 0,
 UNIQUE(workspace_id,kind,key)
);
CREATE TABLE IF NOT EXISTS governance_resource_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 resource_id uuid NOT NULL REFERENCES governance_resources(id) ON DELETE CASCADE,
 version integer NOT NULL CHECK(version>0),
 name text NOT NULL,
 description text NOT NULL DEFAULT '',
 payload jsonb NOT NULL DEFAULT '{}',
 status text NOT NULL CHECK(status IN ('draft','published','disabled')),
 content_hash text NOT NULL,
 created_by uuid NOT NULL REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(resource_id,version)
);
CREATE TABLE IF NOT EXISTS governance_model_profiles (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 current_version integer NOT NULL DEFAULT 0,
 deleted boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS governance_model_profile_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 profile_id uuid NOT NULL REFERENCES governance_model_profiles(id) ON DELETE CASCADE,
 version integer NOT NULL,
 name text NOT NULL,
 provider text NOT NULL,
 base_url text NOT NULL DEFAULT '',
 encrypted_key bytea NOT NULL DEFAULT '',
 chat_models jsonb NOT NULL DEFAULT '[]',
 embedding_models jsonb NOT NULL DEFAULT '[]',
 max_tokens integer NOT NULL DEFAULT 4096 CHECK(max_tokens>0),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(profile_id,version)
);
CREATE TABLE IF NOT EXISTS governance_model_routes (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
 version integer NOT NULL,
 routes jsonb NOT NULL,
 created_by uuid NOT NULL REFERENCES users(id),
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,version)
);
CREATE OR REPLACE FUNCTION reject_governance_version_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'governance versions are immutable'; END $$;
CREATE TRIGGER governance_resources_immutable BEFORE UPDATE ON governance_resource_versions
 FOR EACH ROW EXECUTE FUNCTION reject_governance_version_mutation();
CREATE TRIGGER governance_profiles_immutable BEFORE UPDATE ON governance_model_profile_versions
 FOR EACH ROW EXECUTE FUNCTION reject_governance_version_mutation();
CREATE TRIGGER governance_routes_immutable BEFORE UPDATE ON governance_model_routes
 FOR EACH ROW EXECUTE FUNCTION reject_governance_version_mutation();
DO $$ DECLARE table_name text; BEGIN
 FOREACH table_name IN ARRAY ARRAY['governance_resources','governance_resource_versions','governance_model_profiles','governance_model_profile_versions','governance_model_routes','audit_logs'] LOOP
 EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',table_name);
 EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',table_name);
 EXECUTE format('CREATE POLICY %I ON %I USING(workspace_id::text=current_setting(''app.workspace_id'',true)) WITH CHECK(workspace_id::text=current_setting(''app.workspace_id'',true))',table_name||'_scope',table_name);
 END LOOP;
END $$;
