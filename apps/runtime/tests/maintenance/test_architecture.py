from scripts.check_architecture import violations


def test_architecture_rejects_transport_dependencies_and_old_authority(tmp_path):
    module = tmp_path / "go-control-plane/internal/modules/project"
    module.mkdir(parents=True)
    (module / "project.go").write_text(
        'package project\nimport "net/http"\nimport "app/internal/transport/http"\nconst query="SELECT * FROM novels"\n'
    )
    found = violations(tmp_path)
    assert any("imports transport" in failure for failure in found)
    assert any("imports infrastructure" in failure for failure in found)
    assert any("legacy state SQL" in failure for failure in found)


def test_architecture_rejects_sql_and_pool_ownership_in_transport(tmp_path):
    directory = tmp_path / "go-control-plane/internal/transport/http"
    directory.mkdir(parents=True)
    (directory / "handler.go").write_text(
        'package http\nimport "github.com/jackc/pgx/v5/pgxpool"\nconst query="SELECT id FROM projects"\n'
    )
    found = violations(tmp_path)
    assert any("transport contains SQL" in failure for failure in found)
    assert any("owns a database pool" in failure for failure in found)
