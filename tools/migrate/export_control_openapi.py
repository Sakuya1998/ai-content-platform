"""Export the generic Go Control HTTP contract."""

import json
import re
from pathlib import Path


def control_openapi():
    object_schema = {"type": "object", "additionalProperties": True}
    uuid_schema = {"type": "string", "format": "uuid"}
    schemas = {
        "ProjectRequest": {
            "type": "object",
            "required": ["title", "content_type"],
            "properties": {
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "content_type": {"type": "string", "minLength": 1},
                "input": object_schema,
                "brief": object_schema,
            },
        },
        "DocumentRequest": {
            "type": "object",
            "required": ["project_id", "title", "document_type"],
            "properties": {"project_id": uuid_schema, "title": {"type": "string"}, "document_type": {"type": "string"}},
        },
        "ProjectUpdate": {
            "type": "object",
            "required": ["title", "expected_revision"],
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "input": object_schema,
                "expected_revision": {"type": "integer", "minimum": 1},
            },
        },
        "WorkflowRequest": {
            "type": "object",
            "required": ["name", "definition"],
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 120},
                "definition": {
                    "type": "object",
                    "required": ["template", "nodes"],
                    "properties": {
                        "template": {"type": "string"},
                        "nodes": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
        "ContentIR": {
            "type": "object",
            "required": ["schema_version", "blocks"],
            "properties": {"schema_version": {"const": "1.0"}, "blocks": {"type": "array", "items": object_schema}},
        },
        "VersionRequest": {
            "type": "object",
            "required": ["expected_version", "document"],
            "properties": {
                "expected_version": {"type": "integer", "minimum": 0},
                "document": {"$ref": "#/components/schemas/ContentIR"},
            },
        },
        "JobRequest": {
            "type": "object",
            "required": ["project_id", "workflow", "workflow_version"],
            "properties": {
                "project_id": uuid_schema,
                "workflow": {"type": "string", "pattern": "^[^@]+$"},
                "workflow_version": {"type": "integer", "minimum": 1},
                "operation": {"type": "string"},
                "arguments": object_schema,
                "document_id": uuid_schema,
                "document_type": {"type": "string"},
                "title": {"type": "string"},
                "objective": {"type": "string"},
                "source_version_id": uuid_schema,
                "input_version_ids": {"type": "array", "items": uuid_schema},
                "nodes": {"type": "array", "items": {"type": "string"}},
            },
        },
        "ResumeRequest": {
            "type": "object",
            "required": ["action"],
            "properties": {"action": {"enum": ["approve", "revise", "reconcile"]}, "feedback": {"type": "string"}},
        },
        "BriefRequest": {
            "type": "object",
            "required": ["brief", "expected_version"],
            "properties": {
                "brief": object_schema,
                "expected_version": {"type": "integer", "minimum": 1},
                "change_summary": {"type": "string", "maxLength": 500},
            },
        },
        "Job": {
            "type": "object",
            "properties": {
                "job_id": uuid_schema,
                "project_id": uuid_schema,
                "status": {
                    "enum": [
                        "queued",
                        "running",
                        "waiting_review",
                        "cancel_requested",
                        "completed",
                        "failed",
                        "cancelled",
                    ]
                },
                "sequence": {"type": "integer", "minimum": 0},
            },
        },
        "EvaluationRule": {
            "type": "object",
            "required": ["key", "dimension", "weight"],
            "properties": {
                "key": {"type": "string", "minLength": 1, "maxLength": 120},
                "dimension": {"type": "string", "minLength": 1, "maxLength": 80},
                "weight": {"type": "number", "exclusiveMinimum": 0},
                "critical": {"type": "boolean"},
                "parameters": {"type": "object", "maxProperties": 64, "additionalProperties": True},
            },
        },
        "EvaluationPolicy": {
            "type": "object",
            "required": ["policy_id", "version", "engine_version", "gate_threshold", "critical_threshold", "rules"],
            "properties": {
                "policy_id": {"type": "string", "minLength": 1},
                "version": {"type": "integer", "minimum": 1},
                "engine_version": {"type": "string", "minLength": 1},
                "gate_threshold": {"type": "number", "minimum": 0, "maximum": 100},
                "critical_threshold": {"type": "number", "minimum": 0, "maximum": 100},
                "rules": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 32,
                    "items": {"$ref": "#/components/schemas/EvaluationRule"},
                },
            },
        },
        "BenchmarkComparisonRequest": {
            "type": "object",
            "required": ["current_id"],
            "properties": {
                "current_id": {"type": "string", "minLength": 1},
                "threshold": {"type": "number", "minimum": 0, "maximum": 100},
            },
        },
        "BenchmarkRequest": {
            "type": "object",
            "required": ["project_id"],
            "properties": {"project_id": uuid_schema},
            "additionalProperties": True,
        },
    }
    paths = {}

    def add(path, methods, schema=None, status="200"):
        target = paths.setdefault("/api/v2" + path, {})
        parameters = [
            {"name": name, "in": "path", "required": True, "schema": {"type": "string"}}
            for name in re.findall(r"\{([^}]+)\}", path)
        ]
        for method in methods.split():
            operation = {
                "operationId": method + re.sub(r"[^a-zA-Z0-9]", "_", path),
                "parameters": list(parameters),
                "responses": {
                    status if method in {"post", "put"} else "200": {"description": "Success"},
                    "401": {"description": "Authentication required"},
                    "403": {"description": "Workspace access denied"},
                    "404": {"description": "Resource not found"},
                    "409": {"description": "Resource version or idempotency conflict"},
                },
            }
            if schema and method in {"post", "put"}:
                operation["requestBody"] = {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/" + schema}}},
                }
            target[method] = operation

    def add_query_parameters(path, method, parameters):
        paths["/api/v2" + path][method]["parameters"].extend(parameters)

    add("/projects", "get post", "ProjectRequest", "201")
    add("/projects/{project_id}", "get put delete", "ProjectUpdate")
    add("/projects/import", "post", status="201")
    add("/projects/{project_id}/projection", "get")
    add("/projects/{project_id}/brief", "put", "BriefRequest")
    add("/projects/{project_id}/brief/versions", "get")
    add("/projects/{project_id}/jobs", "get")
    for resource in ["blocks/{block_index}", "planning/{artifact_type}"]:
        base = "/projects/{project_id}/" + resource
        for suffix in [
            "versions",
            "versions/diff",
            "versions/{version}",
            "candidates",
            "evaluations",
            "evaluations/compare",
        ]:
            add(base + "/" + suffix, "get")
        for suffix in [
            "versions/{version}/restore",
            "versions/{version}/evaluate",
            "candidates",
            "candidates/{candidate_id}/select",
        ]:
            add(base + "/" + suffix, "post", status="202")
        add(base + "/evaluations/{evaluation_id}/baseline", "put")
    for suffix in ["audits", "conflicts", "memory", "memory/quality", "usage", "traces", "export"]:
        add("/projects/{project_id}/" + suffix, "get")
    for suffix in ["memory/rebuild", "memory/evaluate", "memory/search", "memory/refresh", "export/jobs"]:
        add("/projects/{project_id}/" + suffix, "post", status="202")
    add("/documents", "get post", "DocumentRequest", "201")
    add("/documents/{document_id}", "get")
    add("/documents/{document_id}/versions", "get post", "VersionRequest", "201")
    add("/documents/{document_id}/versions/{version}", "get")
    add("/documents/{document_id}/versions/diff", "get")
    add("/content/versions/{version_id}", "get")
    add("/jobs", "get post", "JobRequest", "202")
    add("/jobs/{job_id}", "get")
    add("/jobs/{job_id}/cancel", "post", status="202")
    add("/jobs/{job_id}/resume", "post", "ResumeRequest", "202")
    add("/jobs/{job_id}/events", "get")
    paths["/api/v2/jobs/{job_id}/events"]["get"]["responses"]["200"]["content"] = {
        "text/event-stream": {"schema": {"type": "string"}}
    }
    add("/transfers/{transfer_id}", "get")
    add("/transfers/{transfer_id}/download", "get")
    add("/transfers/{transfer_id}/cancel", "post")
    add("/projects/{project_id}/productions", "get post", status="202")
    add("/projects/{project_id}/productions/{production_id}", "get")
    for suffix in ["resume", "cancel", "advance"]:
        add("/projects/{project_id}/productions/{production_id}/" + suffix, "post", status="202")
    add("/workspaces/{workspace_id}/workflows", "get post", "WorkflowRequest", "201")
    add("/workspaces/{workspace_id}/workflows/{workflow_id}", "get")
    add("/workspaces/{workspace_id}/workflows/{workflow_id}/publish", "post")
    add("/workspaces/{workspace_id}/workflows/{workflow_id}/disable", "post")
    add("/evaluations/benchmarks", "get post", "BenchmarkRequest", "202")
    add_query_parameters(
        "/evaluations/benchmarks",
        "get",
        [
            {"name": "job_id", "in": "query", "schema": {"type": "string"}},
            {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["completed", "passed", "failed"]}},
        ],
    )
    add("/evaluations/benchmarks/{benchmark_id}", "get")
    add("/evaluations/benchmarks/{baseline_id}/compare", "post", "BenchmarkComparisonRequest")
    add("/evaluations/stats", "get")
    add("/evaluations/trends", "get")
    add("/evaluations/issues", "get")
    add("/evaluations/dimensions", "get")
    add("/evaluations/policies", "get post", "EvaluationPolicy", "201")
    add("/evaluations/policies/{policy_id}/v/{version}", "get")
    add("/evaluations/policies/{policy_id}/v/{version}/publish", "post")
    add("/evaluations/policies/{policy_id}/v/{version}/archive", "post")
    for path, parameters in {
        "/evaluations/stats": [{"name": "since", "in": "query", "schema": {"type": "string", "format": "date-time"}}],
        "/evaluations/trends": [
            {"name": "dimension", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 500}},
        ],
        "/evaluations/issues": [
            {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100}}
        ],
        "/evaluations/dimensions": [
            {"name": "category", "in": "query", "schema": {"type": "string", "enum": ["structure", "content", "style"]}}
        ],
        "/evaluations/policies": [
            {"name": "policy_id", "in": "query", "schema": {"type": "string"}},
            {"name": "status", "in": "query", "schema": {"type": "string", "enum": ["draft", "published", "archived"]}},
        ],
    }.items():
        add_query_parameters(path, "get", parameters)
    return {
        "openapi": "3.1.0",
        "info": {"title": "Agent Control API", "version": "2.0.0"},
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": {"session": {"type": "apiKey", "in": "cookie", "name": "session"}},
        },
        "security": [{"session": []}],
    }


def main():
    destination = Path(__file__).resolve().parents[2] / "docs/api/openapi/control-v2.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(control_openapi(), indent=2) + "\n")


if __name__ == "__main__":
    main()
