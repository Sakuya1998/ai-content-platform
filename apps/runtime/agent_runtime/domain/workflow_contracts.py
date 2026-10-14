"""Registered workflow node contracts; contains no prompt text or local resources."""

NOVEL_NODES = (
    "world_builder",
    "character_designer",
    "plot_planner",
    "scene_planner",
    "scene_writer",
    "scene_rewriter",
    "style_editor",
    "consistency_checker",
    "quality_evaluator",
    "replanner",
    "book_auditor",
    "chapter_digest",
)
CONTENT_NODES = ("content.plan", "content.write", "content.review")
WORKFLOWS = {
    "novel.write@1": (*NOVEL_NODES, "memory.refresh"),
    "content.generate@1": (*CONTENT_NODES, "memory.refresh"),
    "content.adapt@1": (*CONTENT_NODES, "memory.refresh"),
    "memory.refresh@1": ("memory.refresh",),
    "media.video@1": ("media.plan", "media.review", "memory.refresh"),
    "media.image@1": ("media.image.plan", "media.review", "memory.refresh"),
    "media.speech@1": ("media.speech.plan", "media.review", "memory.refresh"),
    "media.music@1": ("media.music.plan", "media.review", "memory.refresh"),
    "media.sound_effect@1": ("media.sound_effect.plan", "media.review", "memory.refresh"),
    "media.storyboard@1": ("media.storyboard.plan", "media.review", "memory.refresh"),
    "media.timeline@1": ("media.timeline.plan", "media.review", "memory.refresh"),
    "media.compose@1": ("memory.refresh",),
    "media.assemble@1": ("memory.refresh",),
}
