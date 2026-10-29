from tools.migrate.platform_bootstrap import database_url_for, ensure_role


class Cursor:
    def __init__(self, exists: bool):
        self.exists = exists
        self.calls = []

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def fetchone(self):
        return (1,) if self.exists else None


class Connection:
    def __init__(self, exists: bool):
        self.cursor_instance = Cursor(exists)

    def cursor(self):
        class Context:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, *args):
                return False

        return Context(self.cursor_instance)


def test_ensure_role_composes_password_as_sql_literal_without_parameter_placeholder():
    connection = Connection(False)
    ensure_role(connection, "runtime", "p@ss word")

    query, params = connection.cursor_instance.calls[-1]
    assert "CREATE ROLE" in query.as_string()
    assert "p@ss word" in query.as_string()
    assert params is None


def test_ensure_role_alters_existing_role_with_literal_password():
    connection = Connection(True)
    ensure_role(connection, "runtime", "secret")

    query, params = connection.cursor_instance.calls[-1]
    assert "ALTER ROLE" in query.as_string()
    assert params is None


def test_database_url_for_keeps_admin_credentials_and_changes_database():
    url = database_url_for("postgresql://admin:secret@db.example.com:5432/postgres", "runtime")

    assert url == "user=admin password=secret dbname=runtime host=db.example.com port=5432"
