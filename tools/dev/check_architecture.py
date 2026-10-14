"""Check Control/Runtime ownership and Go module dependency boundaries."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQL_LEGACY = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:novels|novel_\w+)\b", re.IGNORECASE)
INTERSERVICE = re.compile(r"\b(?:control_api_url|runtime_url|CONTROL_API_URL|RUNTIME_URL)\b")
GO_IMPORT = re.compile(r'"([^"\n]+)"')
MODULE_POSTGRES_IMPORT = re.compile(r"/internal/modules/(.+?)/postgres(?:/|$)")
SQL_STATEMENT = re.compile(r"\b(?:SELECT\s+.+?\bFROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b", re.IGNORECASE)
PRODUCTION_STATE_TABLE = re.compile(r"\bproduction_(?:runs|children)\b")
EVALUATION_REPORT_TABLE = re.compile(r"\bevaluation_benchmark_runs\b")
AUDIT_LOG_INSERT = re.compile(r"\bINSERT\s+INTO\s+audit_logs\b", re.IGNORECASE)
PROJECT_RECORD_WRITE = re.compile(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+project_records\b", re.IGNORECASE)
PROJECT_STATE_WRITE = re.compile(r"\bUPDATE\s+project_projections\s+SET\s+state\b", re.IGNORECASE)
CONTENT_STATE_WRITE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"(?:documents|document_versions|content_blocks|derivations)\b",
    re.IGNORECASE,
)


def violations(root=ROOT):
    failures = []
    control = root / "go-control-plane"
    if (control / "internal/controlplane").exists():
        failures.append("Go: retired internal/controlplane package exists")
    for directory, suffix in [(control, ".go"), (root / "src/agent_runtime", ".py")]:
        for path in directory.rglob("*" + suffix):
            body = path.read_text()
            label = str(path.relative_to(root))
            if suffix == ".go" and "internal/modules/content/transfer" in body:
                failures.append(label + ": Go: retired content/transfer module path")
            production_state_outside_owner = (
                suffix == ".go"
                and not path.name.endswith("_test.go")
                and PRODUCTION_STATE_TABLE.search(body)
                and "/internal/modules/production/postgres/" not in label
            )
            if production_state_outside_owner:
                failures.append(label + ": production state belongs to production module")
            evaluation_report_outside_owner = (
                suffix == ".go"
                and not path.name.endswith("_test.go")
                and EVALUATION_REPORT_TABLE.search(body)
                and "/internal/modules/evaluation/postgres/" not in label
            )
            if evaluation_report_outside_owner:
                failures.append(label + ": evaluation reports belong to evaluation module")
            audit_insert_outside_owner = (
                suffix == ".go"
                and not path.name.endswith("_test.go")
                and AUDIT_LOG_INSERT.search(body)
                and "/internal/modules/identity/postgres/" not in label
            )
            if audit_insert_outside_owner:
                failures.append(label + ": audit log writes belong to identity module")
            project_record_write_outside_owner = (
                suffix == ".go"
                and not path.name.endswith("_test.go")
                and PROJECT_RECORD_WRITE.search(body)
                and "/internal/modules/execution/postgres/" not in label
            )
            if project_record_write_outside_owner:
                failures.append(label + ": project record writes belong to execution projections")
            project_state_write_outside_owner = (
                suffix == ".go"
                and not path.name.endswith("_test.go")
                and PROJECT_STATE_WRITE.search(body)
                and "/internal/modules/execution/postgres/" not in label
            )
            if project_state_write_outside_owner:
                failures.append(label + ": Runtime project state writes belong to execution projections")
            content_write_outside_owner = (
                suffix == ".go"
                and not path.name.endswith("_test.go")
                and CONTENT_STATE_WRITE.search(body)
                and "/internal/modules/content/postgres/" not in label
            )
            if content_write_outside_owner:
                failures.append(label + ": document and content writes belong to content module")
            if path.name.endswith("_test.go"):
                continue
            if "/internal/controlplane" in body:
                failures.append(label + ": retired package import")
            if "/api/v2/novels" in body or "/api/novels" in body:
                failures.append(label + ": retired public resource path")
            if INTERSERVICE.search(body):
                failures.append(label + ": interservice HTTP configuration")
            if SQL_LEGACY.search(body):
                failures.append(label + ": legacy state SQL")
            if suffix == ".go" and "/internal/transport/" in label:
                if SQL_STATEMENT.search(body):
                    failures.append(label + ": transport contains SQL")
                if "pgxpool" in body:
                    failures.append(label + ": transport owns a database pool")
            if suffix == ".go" and "/internal/modules/" in label:
                imports = GO_IMPORT.findall(body)
                if any("/internal/transport/" in item for item in imports):
                    failures.append(label + ": application module imports transport")
                parts = path.relative_to(control).parts
                if len(parts) > 4 and parts[:2] == ("internal", "modules") and "postgres" in parts[3:-1]:
                    source_owner = "/".join(parts[2 : parts.index("postgres", 3)])
                    for item in imports:
                        target = MODULE_POSTGRES_IMPORT.search(item)
                        if target is not None and target.group(1) != source_owner:
                            failures.append(label + ": module imports foreign PostgreSQL adapter " + target.group(1))
                if "/postgres/" not in label and any(
                    item == "net/http" or "jackc/pgx" in item or "aws-sdk" in item for item in imports
                ):
                    failures.append(label + ": domain/application imports infrastructure")
    return failures


def main():
    failures = violations()
    if failures:
        raise SystemExit("\n".join(failures))
    print("Architecture boundaries verified")


if __name__ == "__main__":
    main()
