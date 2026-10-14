ALTER TABLE governance_model_profile_versions
    ADD COLUMN music_models jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(music_models) = 'array'),
    ADD COLUMN sound_effect_models jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(sound_effect_models) = 'array');

ALTER TABLE workflow_prompt_bindings
    DROP CONSTRAINT workflow_prompt_bindings_workflow_check;
ALTER TABLE workflow_prompt_bindings
    ADD CONSTRAINT workflow_prompt_bindings_workflow_check
    CHECK (workflow IN ('novel.write@1','content.generate@1','content.adapt@1','memory.refresh@1','media.video@1','media.image@1','media.speech@1','media.storyboard@1','media.timeline@1','media.compose@1','media.assemble@1','media.music@1','media.sound_effect@1'));

ALTER TABLE workflow_creation_policy_bindings
    DROP CONSTRAINT workflow_creation_policy_bindings_workflow_check;
ALTER TABLE workflow_creation_policy_bindings
    ADD CONSTRAINT workflow_creation_policy_bindings_workflow_check
    CHECK (workflow IN ('novel.write@1','content.generate@1','content.adapt@1','memory.refresh@1','media.video@1','media.image@1','media.speech@1','media.storyboard@1','media.timeline@1','media.compose@1','media.assemble@1','media.music@1','media.sound_effect@1'));
