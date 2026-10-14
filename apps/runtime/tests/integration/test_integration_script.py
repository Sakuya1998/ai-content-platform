from scripts.platform_integration import request_json


def test_integration_script_request_json_uses_json_contract(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"status":"ok"}'

    def fake_urlopen(request, timeout):
        captured["method"] = request.method
        captured["body"] = request.data
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("scripts.platform_integration._opener.open", fake_urlopen)
    status, payload = request_json("http://runtime/readyz", payload={"probe": True}, method="POST")

    assert status == 200
    assert payload == {"status": "ok"}
    assert captured["method"] == "POST"
    assert captured["timeout"] == 5


def test_smoke_submits_idempotent_public_tasks_without_runtime_business_http(monkeypatch, capsys):
    import sys

    from scripts import platform_integration

    calls = []
    task_posts = 0

    def request(url, *, method="GET", payload=None, headers=None):
        nonlocal task_posts
        calls.append((url, method, payload, headers))
        if url.endswith("/api/auth/status"):
            return 200, {"enabled": False}
        if url.endswith("/api/v2/projects"):
            return 201, {"id": "project", "content_type": "screenplay"}
        if url.endswith("/api/v2/jobs"):
            task_posts += 1
            return (409, {"error": "idempotency conflict"}) if task_posts == 3 else (202, {"job_id": "task"})
        if url.endswith("/api/v2/jobs/task"):
            return 200, {"status": "completed"}
        raise AssertionError("unexpected HTTP business route: " + url)

    monkeypatch.setattr(platform_integration, "request_json", request)
    monkeypatch.setattr(platform_integration, "wait_ready", lambda *args: None)
    monkeypatch.setattr(platform_integration, "seed_workspace", lambda *args: None)
    monkeypatch.setattr(platform_integration, "check_tcp", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["platform_integration", "--completion-timeout", "1"])
    assert platform_integration.main() == 0
    posts = [call for call in calls if call[0].endswith("/api/v2/jobs")]
    assert len(posts) == 3 and posts[0][2] == posts[1][2]
    assert posts[0][3]["Idempotency-Key"] == posts[2][3]["Idempotency-Key"]
    assert posts[0][2]["workflow"] == "content.generate"
    assert all("/internal/" not in url for url, *_ in calls)
    assert '"job_id": "task"' in capsys.readouterr().out
