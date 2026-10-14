CREATE TABLE evaluation_benchmark_runs (
    id uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    report jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE evaluation_benchmark_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE evaluation_benchmark_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY benchmark_scope ON evaluation_benchmark_runs
    USING(workspace_id::text=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id::text=current_setting('app.workspace_id',true));
