import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_contract_validator_checks_existing_examples_without_requiring_every_schema_to_have_one():
    result = subprocess.run(
        [sys.executable, "packages/contracts/contracts/validate_examples.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
