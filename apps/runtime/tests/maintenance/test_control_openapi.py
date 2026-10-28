import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "tools" / "migrate"))

from export_control_openapi import control_openapi  # noqa: E402


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


def test_evaluation_management_routes_are_in_the_control_openapi_contract():
    contract = control_openapi()
    assert "/api/v2/evaluations/policies" in contract["paths"]
    assert "/api/v2/evaluations/stats" in contract["paths"]
    assert "/api/v2/evaluations/trends" in contract["paths"]
    assert "/api/v2/evaluations/dimensions" in contract["paths"]
    assert "/api/v2/evaluations/benchmarks/{baseline_id}/compare" in contract["paths"]
    benchmark_filters = contract["paths"]["/api/v2/evaluations/benchmarks"]["get"]["parameters"]
    assert {item["name"] for item in benchmark_filters} >= {"job_id", "status"}
    benchmark_post_parameters = contract["paths"]["/api/v2/evaluations/benchmarks"]["post"]["parameters"]
    assert {item["name"] for item in benchmark_post_parameters} == set()
    policy_post_parameters = contract["paths"]["/api/v2/evaluations/policies"]["post"]["parameters"]
    assert {item["name"] for item in policy_post_parameters} == set()
    assert contract["components"]["schemas"]["EvaluationPolicy"]["properties"]["rules"]["maxItems"] == 32
