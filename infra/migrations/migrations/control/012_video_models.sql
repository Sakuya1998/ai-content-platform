-- Provider-neutral video model registry, frozen by immutable profile versions.
ALTER TABLE governance_model_profile_versions
    ADD COLUMN video_models jsonb NOT NULL DEFAULT '[]'::jsonb
    CHECK (jsonb_typeof(video_models) = 'array');

ALTER TABLE workflow_prompt_bindings
    DROP CONSTRAINT workflow_prompt_bindings_workflow_check;
ALTER TABLE workflow_prompt_bindings
    ADD CONSTRAINT workflow_prompt_bindings_workflow_check
    CHECK (workflow IN ('novel.write@1','content.generate@1','content.adapt@1','memory.refresh@1','media.video@1'));

ALTER TABLE workflow_creation_policy_bindings
    DROP CONSTRAINT workflow_creation_policy_bindings_workflow_check;
ALTER TABLE workflow_creation_policy_bindings
    ADD CONSTRAINT workflow_creation_policy_bindings_workflow_check
    CHECK (workflow IN ('novel.write@1','content.generate@1','content.adapt@1','memory.refresh@1','media.video@1'));
