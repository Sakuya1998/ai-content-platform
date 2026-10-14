-- Durable tenant-scoped production dependency graph and frozen creation context.
CREATE TABLE production_runs (
 id uuid PRIMARY KEY, workspace_id uuid NOT NULL REFERENCES workspaces(id),
 project_id uuid NOT NULL REFERENCES projects(id), idempotency_key text NOT NULL,
 request jsonb NOT NULL, source jsonb NOT NULL, snapshots jsonb NOT NULL,
 status text NOT NULL CHECK(status IN ('queued','assets','assembling','composing','waiting_review','failed','cancelled','completed')),
 error_stage text NOT NULL DEFAULT '', error_code text NOT NULL DEFAULT '',
 timeline_version_id uuid REFERENCES document_versions(id), final_version_id uuid REFERENCES document_versions(id),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(workspace_id,idempotency_key)
);
CREATE TABLE production_children (
 workspace_id uuid NOT NULL REFERENCES workspaces(id), run_id uuid NOT NULL REFERENCES production_runs(id),
 slot text NOT NULL, stage text NOT NULL, shot_id text NOT NULL DEFAULT '', job_id uuid NOT NULL REFERENCES jobs(id),
 PRIMARY KEY(run_id,slot), UNIQUE(job_id)
);
CREATE INDEX production_runs_recovery ON production_runs(workspace_id,status,updated_at);
ALTER TABLE production_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE production_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY production_runs_scope ON production_runs USING(workspace_id=current_setting('app.workspace_id',true)::uuid) WITH CHECK(workspace_id=current_setting('app.workspace_id',true)::uuid);
ALTER TABLE production_children ENABLE ROW LEVEL SECURITY;
ALTER TABLE production_children FORCE ROW LEVEL SECURITY;
CREATE POLICY production_children_scope ON production_children USING(workspace_id=current_setting('app.workspace_id',true)::uuid) WITH CHECK(workspace_id=current_setting('app.workspace_id',true)::uuid);
ALTER TABLE workflow_prompt_bindings DROP CONSTRAINT workflow_prompt_bindings_workflow_check;
ALTER TABLE workflow_prompt_bindings ADD CONSTRAINT workflow_prompt_bindings_workflow_check CHECK(workflow IN ('novel.write@1','content.generate@1','content.adapt@1','memory.refresh@1','media.video@1','media.image@1','media.speech@1','media.storyboard@1','media.timeline@1','media.compose@1','media.assemble@1'));
ALTER TABLE workflow_creation_policy_bindings DROP CONSTRAINT workflow_creation_policy_bindings_workflow_check;
ALTER TABLE workflow_creation_policy_bindings ADD CONSTRAINT workflow_creation_policy_bindings_workflow_check CHECK(workflow IN ('novel.write@1','content.generate@1','content.adapt@1','memory.refresh@1','media.video@1','media.image@1','media.speech@1','media.storyboard@1','media.timeline@1','media.compose@1','media.assemble@1'));
