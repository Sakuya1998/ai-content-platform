"""Validate contract examples with the optional jsonschema package."""

import json
from pathlib import Path


def main() -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise SystemExit("Install jsonschema to validate contracts: python -m pip install jsonschema") from exc

    root = Path(__file__).parent
    for example_path in sorted((root / "examples").glob("*.json")):
        schema_path = root / f"{example_path.stem}.schema.json"
        if not schema_path.is_file():
            raise SystemExit(f"{example_path.name}: matching schema is missing")
        schema = json.loads(schema_path.read_text())
        example = json.loads(example_path.read_text())
        errors = sorted(Draft202012Validator(schema).iter_errors(example), key=str)
        if errors:
            raise SystemExit(f"{example_path.name}: {errors[0].message}")
        print(f"validated {schema_path.name}")


if __name__ == "__main__":
    main()
