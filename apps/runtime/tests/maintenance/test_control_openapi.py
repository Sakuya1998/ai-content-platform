from scripts.export_control_openapi import control_openapi


def test_control_contract_uses_shared_resources_and_explicit_workflows():
    contract = control_openapi()
    paths = contract["paths"]
    assert all("/novels" not in path and "/internal/" not in path for path in paths)
    assert "/api/v2/jobs/{job_id}/resume" in paths
    assert "/api/v2/projects/{project_id}/productions/{production_id}/advance" in paths
    assert "/api/v2/projects/{project_id}/productions/{production_id}/retry" not in paths
    assert "/api/v2/projects/{project_id}/blocks/{block_index}/candidates/{candidate_id}/select" in paths
    assert contract["components"]["schemas"]["JobRequest"]["required"] == ["project_id", "workflow", "workflow_version"]
    assert "content_type" in contract["components"]["schemas"]["ProjectRequest"]["properties"]
