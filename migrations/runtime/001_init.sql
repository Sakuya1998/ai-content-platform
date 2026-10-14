-- Runtime DB: execution state, memory, tool calls and runtime events.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS runtime_jobs (
    id text PRIMARY KEY,
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    command_id text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN ('accepted', 'running', 'waiting_review', 'completed', 'failed', 'cancel_requested', 'cancelled')),
    state_version bigint NOT NULL DEFAULT 1,
    workflow_generation bigint NOT NULL DEFAULT 1,
    fencing_token bigint NOT NULL DEFAULT 0,
    lease_owner text,
    lease_expires_at timestamptz,
    heartbeat_at timestamptz,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    error jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS command_inbox (
    command_id text PRIMARY KEY,
    workspace_id text NOT NULL,
    job_id text NOT NULL,
    payload jsonb NOT NULL,
    accepted_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runtime_event_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id text NOT NULL UNIQUE,
    workspace_id text NOT NULL,
    job_id text NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    sequence bigint NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    published_at timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, sequence)
);

CREATE TABLE IF NOT EXISTS execution_plans (
    job_id text PRIMARY KEY REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    capability_snapshot_hash text NOT NULL,
    plan jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS memory_nodes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    document_version_id uuid,
    memory_type text NOT NULL,
    source_id text NOT NULL,
    source_version bigint NOT NULL,
    content text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, memory_type, source_id, source_version)
);

CREATE TABLE IF NOT EXISTS memory_edges (
    from_node_id uuid NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
    to_node_id uuid NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
    edge_type text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (from_node_id, to_node_id, edge_type)
);

CREATE TABLE IF NOT EXISTS canonical_facts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id text NOT NULL,
    project_id text NOT NULL,
    fact_type text NOT NULL,
    subject text NOT NULL,
    value jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('planned', 'draft', 'confirmed', 'deprecated')),
    evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
    version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS context_packs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id text NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    task_id text NOT NULL,
    canon_version bigint NOT NULL,
    source_version_ids uuid[] NOT NULL DEFAULT '{}',
    retrieved_memory_ids uuid[] NOT NULL DEFAULT '{}',
    token_budget integer NOT NULL CHECK (token_budget > 0),
    content_hash text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, task_id, content_hash)
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id uuid NOT NULL REFERENCES memory_nodes(id) ON DELETE CASCADE,
    project_id text NOT NULL,
    embedding_model text NOT NULL,
    index_version text NOT NULL,
    embedding vector(1536) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (id, embedding_model, index_version)
);

CREATE INDEX IF NOT EXISTS runtime_jobs_claim_idx ON runtime_jobs (status, lease_expires_at, created_at);
CREATE INDEX IF NOT EXISTS memory_nodes_project_idx ON memory_nodes (project_id, memory_type, created_at DESC);
CREATE INDEX IF NOT EXISTS memory_embeddings_hnsw_idx ON memory_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS tool_calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id text NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    tool_id text NOT NULL,
    tool_version text NOT NULL,
    operation_id text NOT NULL,
    input_hash text NOT NULL,
    output_hash text,
    side_effect_level text NOT NULL,
    status text NOT NULL,
    latency_ms integer,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, operation_id)
);

CREATE TABLE IF NOT EXISTS prompt_renderings (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id text NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    prompt_pack_id text NOT NULL,
    prompt_version text NOT NULL,
    context_hash text NOT NULL,
    input_hash text NOT NULL,
    output_schema text NOT NULL,
    token_count integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mcp_calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id text NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    policy_id text NOT NULL,
    server_id text NOT NULL,
    tool_name text NOT NULL,
    operation_id text NOT NULL,
    input_hash text NOT NULL,
    output_hash text,
    status text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, operation_id)
);

CREATE TABLE IF NOT EXISTS model_calls (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id text NOT NULL REFERENCES runtime_jobs(id) ON DELETE CASCADE,
    provider text NOT NULL,
    model text NOT NULL,
    prompt_hash text NOT NULL,
    input_tokens integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    cost numeric(18, 8) NOT NULL DEFAULT 0,
    status text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE runtime_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE command_inbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE runtime_event_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE execution_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE canonical_facts ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_edges ENABLE ROW LEVEL SECURITY;
ALTER TABLE context_packs ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_embeddings ENABLE ROW LEVEL SECURITY;
ALTER TABLE tool_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_renderings ENABLE ROW LEVEL SECURITY;
ALTER TABLE mcp_calls ENABLE ROW LEVEL SECURITY;
ALTER TABLE model_calls ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS runtime_jobs_workspace_policy ON runtime_jobs;
CREATE POLICY runtime_jobs_workspace_policy ON runtime_jobs
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
DROP POLICY IF EXISTS memory_nodes_workspace_policy ON memory_nodes;
CREATE POLICY memory_nodes_workspace_policy ON memory_nodes
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
DROP POLICY IF EXISTS canonical_facts_workspace_policy ON canonical_facts;
CREATE POLICY canonical_facts_workspace_policy ON canonical_facts
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));

ALTER TABLE runtime_jobs FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_nodes FORCE ROW LEVEL SECURITY;
ALTER TABLE canonical_facts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS command_inbox_workspace_policy ON command_inbox;
CREATE POLICY command_inbox_workspace_policy ON command_inbox
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
DROP POLICY IF EXISTS runtime_event_outbox_workspace_policy ON runtime_event_outbox;
CREATE POLICY runtime_event_outbox_workspace_policy ON runtime_event_outbox
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
DROP POLICY IF EXISTS execution_plans_workspace_policy ON execution_plans;
CREATE POLICY execution_plans_workspace_policy ON execution_plans
    USING (EXISTS (SELECT 1 FROM runtime_jobs WHERE runtime_jobs.id = execution_plans.job_id
                   AND runtime_jobs.workspace_id::text = current_setting('app.workspace_id', true)))
    WITH CHECK (EXISTS (SELECT 1 FROM runtime_jobs WHERE runtime_jobs.id = execution_plans.job_id
                        AND runtime_jobs.workspace_id::text = current_setting('app.workspace_id', true)));
DROP POLICY IF EXISTS memory_edges_workspace_policy ON memory_edges;
CREATE POLICY memory_edges_workspace_policy ON memory_edges
    USING (EXISTS (SELECT 1 FROM memory_nodes WHERE memory_nodes.id = memory_edges.from_node_id
                   AND memory_nodes.workspace_id = current_setting('app.workspace_id', true)));
DROP POLICY IF EXISTS context_packs_workspace_policy ON context_packs;
CREATE POLICY context_packs_workspace_policy ON context_packs
    USING (EXISTS (SELECT 1 FROM runtime_jobs WHERE runtime_jobs.id = context_packs.job_id
                   AND runtime_jobs.workspace_id::text = current_setting('app.workspace_id', true)));
DROP POLICY IF EXISTS memory_embeddings_workspace_policy ON memory_embeddings;
CREATE POLICY memory_embeddings_workspace_policy ON memory_embeddings
    USING (EXISTS (SELECT 1 FROM memory_nodes WHERE memory_nodes.id = memory_embeddings.id
                   AND memory_nodes.workspace_id = current_setting('app.workspace_id', true)));
DROP POLICY IF EXISTS tool_calls_workspace_policy ON tool_calls;
CREATE POLICY tool_calls_workspace_policy ON tool_calls
    USING (EXISTS (SELECT 1 FROM runtime_jobs WHERE runtime_jobs.id = tool_calls.job_id
                   AND runtime_jobs.workspace_id::text = current_setting('app.workspace_id', true)));
DROP POLICY IF EXISTS prompt_renderings_workspace_policy ON prompt_renderings;
CREATE POLICY prompt_renderings_workspace_policy ON prompt_renderings
    USING (EXISTS (SELECT 1 FROM runtime_jobs WHERE runtime_jobs.id = prompt_renderings.job_id
                   AND runtime_jobs.workspace_id::text = current_setting('app.workspace_id', true)));
DROP POLICY IF EXISTS mcp_calls_workspace_policy ON mcp_calls;
CREATE POLICY mcp_calls_workspace_policy ON mcp_calls
    USING (EXISTS (SELECT 1 FROM runtime_jobs WHERE runtime_jobs.id = mcp_calls.job_id
                   AND runtime_jobs.workspace_id::text = current_setting('app.workspace_id', true)));
DROP POLICY IF EXISTS model_calls_workspace_policy ON model_calls;
CREATE POLICY model_calls_workspace_policy ON model_calls
    USING (EXISTS (SELECT 1 FROM runtime_jobs WHERE runtime_jobs.id = model_calls.job_id
                   AND runtime_jobs.workspace_id::text = current_setting('app.workspace_id', true)));

ALTER TABLE command_inbox FORCE ROW LEVEL SECURITY;
ALTER TABLE runtime_event_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE execution_plans FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_edges FORCE ROW LEVEL SECURITY;
ALTER TABLE context_packs FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_embeddings FORCE ROW LEVEL SECURITY;
ALTER TABLE tool_calls FORCE ROW LEVEL SECURITY;
ALTER TABLE prompt_renderings FORCE ROW LEVEL SECURITY;
ALTER TABLE mcp_calls FORCE ROW LEVEL SECURITY;
ALTER TABLE model_calls FORCE ROW LEVEL SECURITY;
