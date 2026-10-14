"""Exercise mqadmin's misleading exit-code behavior through the real init script."""

import os
import subprocess
from pathlib import Path


def test_init_retries_admin_success_exit_without_created_resource(tmp_path):
    home = tmp_path / "rocketmq"
    (home / "bin").mkdir(parents=True)
    state = tmp_path / "attempts"
    (home / "bin" / "mqadmin").write_text(
        "#!/bin/sh\n"
        'if [ ! -f "$MQ_INIT_TEST_STATE" ]; then\n'
        '  touch "$MQ_INIT_TEST_STATE"\n'
        '  echo "[error] clusterName does not exist"\n'
        "  exit 0\n"
        "fi\n"
        'case "$1" in\n'
        'updateTopic) echo "create topic to test:10911 success.";;\n'
        'updateSubGroup) echo "create subscription group to test:10911 success.";;\n'
        "esac\n"
    )
    result = subprocess.run(
        ["sh", "deploy/rocketmq/init.sh"],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "ROCKETMQ_HOME": str(home), "MQ_INIT_TEST_STATE": str(state)},
    )
    assert result.returncode == 0
    assert result.stdout.count("create topic to test:10911 success.") == 4
    assert result.stdout.count("create subscription group to test:10911 success.") == 5


def test_deployment_manifest_contains_independent_runtime_messaging_roles():
    import yaml

    documents = list(yaml.safe_load_all(Path("deploy/k8s/messaging.yaml").read_text()))
    roles = {
        doc["metadata"]["name"]: doc["spec"]["template"]["spec"]["containers"][0]["command"][-1] for doc in documents
    }
    assert set(roles.values()) == {
        "dispatch",
        "control",
        "relay",
        "memory",
        "memory-scheduler",
        "worker",
        "memory-worker",
    }
    for document in documents:
        if roles[document["metadata"]["name"]] in {"worker", "memory-worker"}:
            assert document["spec"]["replicas"] == 0
