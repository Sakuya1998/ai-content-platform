-- Timeline planning and deterministic composition retain the frozen governance contract.
ALTER TABLE workflow_prompt_bindings
    DROP CONSTRAINT workflow_prompt_bindings_workflow_check;
ALTER TABLE workflow_prompt_bindings
    ADD CONSTRAINT workflow_prompt_bindings_workflow_check
    CHECK (workflow IN ('novel.write@1','content.generate@1','content.adapt@1','memory.refresh@1','media.video@1','media.image@1','media.speech@1','media.storyboard@1','media.timeline@1','media.compose@1'));

ALTER TABLE workflow_creation_policy_bindings
    DROP CONSTRAINT workflow_creation_policy_bindings_workflow_check;
ALTER TABLE workflow_creation_policy_bindings
    ADD CONSTRAINT workflow_creation_policy_bindings_workflow_check
    CHECK (workflow IN ('novel.write@1','content.generate@1','content.adapt@1','memory.refresh@1','media.video@1','media.image@1','media.speech@1','media.storyboard@1','media.timeline@1','media.compose@1'));
