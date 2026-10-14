"""Explicit policy fixtures; production defaults are database seed resources."""


def policy_payload():
    return {
        "schema_version": "creation-policy@1",
        "canon": {
            "entity_types": ["entity", "content_unit"],
            "fact_types": ["event", "state", "other"],
            "require_evidence": True,
            "max_audit_entries": 200,
        },
        "memory": {
            "group_size": 5,
            "leaf_summary_chars": 500,
            "group_summary_chars": 1800,
            "document_summary_chars": 6000,
            "neighbor_window": 5,
        },
        "constraints": {
            "schema": {"type": "object", "properties": {"title": {"type": "string"}}, "additionalProperties": False},
            "defaults": {"title": ""},
            "labels": {"title": "Title"},
            "max_prompt_chars": 3500,
        },
        "evaluation": {
            "engine_version": "checks@1",
            "gate_threshold": 70,
            "critical_threshold": 40,
            "rules": [
                {"key": "text.length@1", "dimension": "length", "weight": 1, "critical": False, "parameters": {}}
            ],
        },
    }
