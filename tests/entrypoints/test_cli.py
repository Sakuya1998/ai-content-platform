import json

import httpx

from agent_runtime.entrypoints.cli import main


def test_remote_cli_uses_login_csrf_and_go_task_api(monkeypatch, capsys):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, json={"csrf_token": "csrf"})
        if request.url.path == "/api/v2/projects":
            assert request.headers["x-csrf-token"] == "csrf"
            assert json.loads(request.content)["content_type"] == "novel"
            return httpx.Response(201, json={"id": "project"})
        if request.url.path == "/api/v2/jobs":
            assert json.loads(request.content)["workflow"] == "novel.write"
            return httpx.Response(202, json={"job_id": "job"})
        return httpx.Response(200, json={"status": "waiting_review"})

    real = httpx.Client
    monkeypatch.setattr(
        "agent_runtime.entrypoints.cli.httpx.Client",
        lambda **kwargs: real(**kwargs, transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setenv("PLATFORM_CLI_PASSWORD", "private-password")
    main(
        ["--platform-url", "http://control.test", "--identifier", "owner", "--title", "Novel", "--inspiration", "story"]
    )
    output = capsys.readouterr().out
    assert "status=waiting_review" in output and "private-password" not in output
    assert all("sqlite" not in str(request.url) for request in requests)
