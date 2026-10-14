"""Service-independent object keys for the shared content bucket."""

import re

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_CATEGORIES = {"uploads", "documents", "assets", "exports"}
_VERSIONED = {"documents", "assets"}


def object_key(
    workspace_id: str,
    project_id: str,
    category: str,
    resource_id: str,
    filename: str,
    *,
    version_id: str | None = None,
) -> str:
    if category not in _CATEGORIES:
        raise ValueError("unsupported object category")
    if category in _VERSIONED and not version_id:
        raise ValueError("documents and assets require a version ID")
    if category not in _VERSIONED and version_id is not None:
        raise ValueError("uploads and exports do not accept a version ID")
    segments = [workspace_id, project_id, resource_id, filename]
    if version_id is not None:
        segments.append(version_id)
    if any(not isinstance(value, str) or not _SEGMENT.fullmatch(value) for value in segments):
        raise ValueError("object key segments must be safe IDs or filenames")
    base = f"workspaces/{workspace_id}/projects/{project_id}/{category}/{resource_id}"
    if version_id is not None:
        base += f"/versions/{version_id}"
    return f"{base}/{filename}"
