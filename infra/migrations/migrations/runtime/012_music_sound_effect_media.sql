ALTER TABLE generated_media_tasks
    DROP CONSTRAINT generated_media_tasks_modality_check;
ALTER TABLE generated_media_tasks
    ADD CONSTRAINT generated_media_tasks_modality_check
    CHECK (modality IN ('image','audio','music','effect'));
