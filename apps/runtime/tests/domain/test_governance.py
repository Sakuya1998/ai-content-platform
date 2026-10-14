import pytest

from agent_runtime.domain.memory import MemoryNode, ProjectMemoryStore
from agent_runtime.domain.registry import CapabilityCatalog, CapabilityStatus, CapabilityVisibility


def test_catalog_publishes_platform_capability_and_workspace_can_clone_it():
    catalog = CapabilityCatalog()
    platform_prompt = catalog.register(
        kind="prompt_pack",
        key="screenplay",
        version=1,
        visibility=CapabilityVisibility.PLATFORM,
        payload={"output_schema": "screenplay.v1"},
    )
    published = catalog.publish(platform_prompt.id)
    clone = catalog.clone_for_workspace(published.id, "workspace-1")

    assert published.status is CapabilityStatus.PUBLISHED
    assert clone.workspace_id == "workspace-1"
    assert clone.parent_id == published.id
    assert clone.status is CapabilityStatus.DRAFT


def test_catalog_rejects_unpublished_capability_in_snapshot():
    catalog = CapabilityCatalog()
    draft = catalog.register(
        kind="tool",
        key="dangerous",
        version=1,
        visibility=CapabilityVisibility.PLATFORM,
        payload={"side_effect_level": "external"},
    )

    with pytest.raises(ValueError, match="published"):
        catalog.snapshot(
            workspace_id="workspace-1",
            workflow="copy-to-video@1",
            capability_ids=[draft.id],
            schema_versions={"content_ir": "1.0"},
        )


def test_project_memory_isolated_until_knowledge_pack_is_explicitly_shared():
    store = ProjectMemoryStore()
    store.commit(
        MemoryNode(
            project_id="project-a",
            workspace_id="workspace-1",
            memory_type="canon",
            source_id="fact-1",
            source_version=1,
            content="主角害怕火焰",
        )
    )

    assert store.list_for_project("project-b", "workspace-1") == []
    pack = store.publish_knowledge_pack("project-a", "workspace-1", "shared-canon")
    store.share_pack(pack.id, "project-b", "workspace-1")
    shared = store.list_for_project("project-b", "workspace-1")

    assert len(shared) == 1
    assert shared[0].source_id == "fact-1"


def test_knowledge_pack_cannot_cross_workspace():
    store = ProjectMemoryStore()
    pack = store.publish_knowledge_pack("project-a", "workspace-1", "shared-canon")

    with pytest.raises(ValueError, match="workspace"):
        store.share_pack(pack.id, "project-b", "workspace-2")
