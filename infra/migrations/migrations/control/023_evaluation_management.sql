ALTER TABLE evaluation_benchmark_runs
    ADD COLUMN job_id text,
    ADD COLUMN policy_version text,
    ADD COLUMN overall_score numeric(5,2) CHECK (overall_score IS NULL OR overall_score BETWEEN 0 AND 100),
    ADD COLUMN status text NOT NULL DEFAULT 'completed';

CREATE INDEX evaluation_benchmarks_job_idx
    ON evaluation_benchmark_runs(workspace_id, job_id);
CREATE INDEX evaluation_benchmarks_score_idx
    ON evaluation_benchmark_runs(workspace_id, overall_score DESC);
CREATE INDEX evaluation_benchmarks_trends_idx
    ON evaluation_benchmark_runs(workspace_id, created_at DESC, overall_score)
    WHERE status = 'completed';
CREATE INDEX evaluation_benchmarks_report_gin_idx
    ON evaluation_benchmark_runs USING GIN (report jsonb_path_ops);

CREATE TABLE evaluation_policies (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    policy_id text NOT NULL CHECK (policy_id <> ''),
    version integer NOT NULL CHECK (version > 0),
    engine_version text NOT NULL CHECK (engine_version <> ''),
    gate_threshold numeric(5,2) NOT NULL CHECK (gate_threshold BETWEEN 0 AND 100),
    critical_threshold numeric(5,2) NOT NULL CHECK (critical_threshold BETWEEN 0 AND 100),
    rules jsonb NOT NULL CHECK (jsonb_typeof(rules) = 'array'),
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, policy_id, version),
    CHECK (status = 'draft' OR published_at IS NOT NULL)
);

CREATE INDEX evaluation_policies_status_idx
    ON evaluation_policies(workspace_id, policy_id, status);
CREATE UNIQUE INDEX evaluation_policies_one_published_idx
    ON evaluation_policies(workspace_id, policy_id)
    WHERE status = 'published';
ALTER TABLE evaluation_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE evaluation_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY evaluation_policies_workspace ON evaluation_policies
    USING (workspace_id::text = current_setting('app.workspace_id', true))
    WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));

CREATE TABLE evaluation_dimensions (
    key text PRIMARY KEY CHECK (key <> ''),
    display_name text NOT NULL CHECK (display_name <> ''),
    category text NOT NULL CHECK (category IN ('structure', 'content', 'style')),
    description text,
    check_keys jsonb NOT NULL CHECK (jsonb_typeof(check_keys) = 'array'),
    is_builtin boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO evaluation_dimensions (key, display_name, category, description, check_keys, is_builtin) VALUES
    ('length', '长度匹配度', 'structure', '内容长度与目标的匹配程度', '["text.length@1"]', true),
    ('structure', '结构完整性', 'structure', '内容结构的完整性', '["text.structure@1"]', true),
    ('repetition', '重复控制', 'content', '内容重复度控制', '["text.repetition@1"]', true),
    ('consistency', '一致性', 'content', '内容一致性检查', '["text.consistency@1"]', true);

GRANT SELECT, INSERT, UPDATE ON evaluation_policies TO control_app;
GRANT SELECT, INSERT ON evaluation_dimensions TO control_app;
GRANT SELECT, INSERT, UPDATE ON evaluation_benchmark_runs TO control_app;
