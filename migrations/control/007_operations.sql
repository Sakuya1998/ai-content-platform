CREATE TABLE legacy_import_rows (
    source_hash text NOT NULL,
    source_table text NOT NULL,
    source_key text NOT NULL,
    workspace_id uuid REFERENCES workspaces(id),
    payload jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(source_hash,source_table,source_key)
);
CREATE TABLE platform_operation_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id uuid NOT NULL REFERENCES workspaces(id),
    operation text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE legacy_import_rows ENABLE ROW LEVEL SECURITY;
ALTER TABLE legacy_import_rows FORCE ROW LEVEL SECURITY;
CREATE POLICY import_rows_scope ON legacy_import_rows
    USING(workspace_id::text=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id::text=current_setting('app.workspace_id',true));
ALTER TABLE platform_operation_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_operation_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY operation_runs_scope ON platform_operation_runs
    USING(workspace_id::text=current_setting('app.workspace_id',true))
    WITH CHECK(workspace_id::text=current_setting('app.workspace_id',true));
