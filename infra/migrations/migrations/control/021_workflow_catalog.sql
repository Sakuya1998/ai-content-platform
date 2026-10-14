WITH seeds(name,definition) AS (VALUES
('novel.write@1','{"nodes":["world_builder","character_designer","plot_planner","scene_planner","scene_writer","scene_rewriter","consistency_checker","style_editor","quality_evaluator","replanner","chapter_digest","book_auditor","memory.refresh"],"template":"novel.write@1"}'),
('content.generate@1','{"nodes":["content.plan","content.write","content.review","memory.refresh"],"template":"content.generate@1"}'),
('content.adapt@1','{"nodes":["content.plan","content.write","content.review","memory.refresh"],"template":"content.adapt@1"}'),
('memory.refresh@1','{"nodes":["memory.refresh"],"template":"memory.refresh@1"}'),
('media.video@1','{"nodes":["media.plan","media.review","memory.refresh"],"template":"media.video@1"}'),
('media.image@1','{"nodes":["media.image.plan","media.review","memory.refresh"],"template":"media.image@1"}'),
('media.speech@1','{"nodes":["media.speech.plan","media.review","memory.refresh"],"template":"media.speech@1"}'),
('media.music@1','{"nodes":["media.music.plan","media.review","memory.refresh"],"template":"media.music@1"}'),
('media.sound_effect@1','{"nodes":["media.sound_effect.plan","media.review","memory.refresh"],"template":"media.sound_effect@1"}'),
('media.storyboard@1','{"nodes":["media.storyboard.plan","media.review","memory.refresh"],"template":"media.storyboard@1"}'),
('media.timeline@1','{"nodes":["media.timeline.plan","media.review","memory.refresh"],"template":"media.timeline@1"}'),
('media.compose@1','{"nodes":["memory.refresh"],"template":"media.compose@1"}'),
('media.assemble@1','{"nodes":["memory.refresh"],"template":"media.assemble@1"}')
)
INSERT INTO workflow_templates(workspace_id,name,version,status,definition,content_hash)
SELECT NULL,name,1,'published',definition::jsonb,encode(digest(convert_to(definition,'UTF8'),'sha256'),'hex') FROM seeds
WHERE NOT EXISTS(SELECT 1 FROM workflow_templates w WHERE w.workspace_id IS NULL AND w.name=seeds.name AND w.version=1);

ALTER TABLE workflow_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE workflow_templates FORCE ROW LEVEL SECURITY;
CREATE POLICY workflow_templates_read ON workflow_templates FOR SELECT
USING(workspace_id IS NULL OR workspace_id::text=current_setting('app.workspace_id',true));
CREATE POLICY workflow_templates_write ON workflow_templates FOR ALL
USING(workspace_id::text=current_setting('app.workspace_id',true))
WITH CHECK(workspace_id::text=current_setting('app.workspace_id',true));

CREATE OR REPLACE FUNCTION protect_workflow_definition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NEW.id<>OLD.id OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id OR NEW.name<>OLD.name
 OR NEW.version<>OLD.version OR NEW.definition<>OLD.definition OR NEW.content_hash<>OLD.content_hash
 OR NEW.created_at<>OLD.created_at THEN
 RAISE EXCEPTION 'workflow definitions are immutable; create a new version';
 END IF;
 IF OLD.status='disabled' AND NEW.status<>'disabled' OR OLD.status='published' AND NEW.status='draft' THEN
 RAISE EXCEPTION 'workflow publication cannot be reversed';
 END IF;
 RETURN NEW;
END;
$$;
CREATE TRIGGER workflow_definition_immutable BEFORE UPDATE ON workflow_templates
FOR EACH ROW EXECUTE FUNCTION protect_workflow_definition();
