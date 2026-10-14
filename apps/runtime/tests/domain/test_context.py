from agent_runtime.domain.context import ContextAssembler, ContextInput


def test_context_assembler_prioritizes_core_blocks_within_budget_and_records_hashes():
    assembler = ContextAssembler(token_budget=8)
    pack = assembler.assemble(
        [
            ContextInput(kind="retrieval", content="retrieved lore", source="vector:1"),
            ContextInput(kind="current", content="current scene", source="chapter:2"),
            ContextInput(kind="constraints", content="no death", source="brief:1"),
            ContextInput(kind="canon", content="Mira is alive", source="canon:7"),
        ]
    )

    assert [block.kind for block in pack.blocks] == ["canon", "constraints", "current"]
    assert pack.token_count <= 8
    assert all(len(block.content_hash) == 64 for block in pack.blocks)
    assert [block.source for block in pack.blocks] == ["canon:7", "brief:1", "chapter:2"]
    assert len(pack.context_hash) == 64


def test_context_assembly_is_deterministic_for_same_inputs():
    assembler = ContextAssembler(token_budget=100)
    inputs = [
        ContextInput(kind="retrieval", content="fact", source="memory:a"),
        ContextInput(kind="canon", content="rule", source="canon:1"),
    ]

    assert assembler.assemble(inputs).model_dump() == assembler.assemble(inputs).model_dump()


def test_context_budget_counts_cjk_characters_instead_of_one_whitespace_token():
    pack = ContextAssembler(token_budget=4).assemble(
        [
            ContextInput(kind="canon", content="甲乙丙丁戊", source="canon:1"),
        ]
    )

    assert pack.blocks == ()
