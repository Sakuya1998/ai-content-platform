"""工作区资源的受控 payload 规范化。

资源 payload 会进入创作提示词或质量门禁，因此这里不接受任意 prompt、脚本或
未知字段。每种资源只保留领域允许的字段，并在落库前做长度和数值边界限制。
"""

from typing import Any

from agent_runtime.workflows.novel.resources.brief import normalize_creative_brief
from agent_runtime.workflows.novel.resources.styles import STYLE_PROFILES

RESOURCE_KINDS = (
    "content_types",
    "styles",
    "creative_templates",
    "quality_policies",
)
QUALITY_DIMENSIONS = (
    "plot_coherence",
    "character_arc",
    "theme_payoff",
    "style_consistency",
    "ending_satisfaction",
    "unresolved_promises",
)
STYLE_PAYLOAD_FIELDS = (
    "syntax",
    "sentence_length",
    "vocabulary",
    "narrative_techniques",
    "pacing",
    "examples",
)


class ResourcePayloadError(ValueError):
    """资源 payload 不符合受控 schema。"""


def _clean_text(value: Any, *, max_length: int) -> str:
    return " ".join(str(value or "").strip().split())[:max_length]


def _clean_list(value: Any, *, max_items: int, max_length: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _clean_text(item, max_length=max_length)
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        result.append(text)
        if len(result) >= max_items:
            break
    return result


def normalize_content_type_payload(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    parent_id = _clean_text(source.get("parent_id"), max_length=120) or None
    return {
        "parent_id": parent_id,
        "aliases": _clean_list(source.get("aliases"), max_items=12, max_length=80),
        "tags": _clean_list(source.get("tags"), max_items=20, max_length=50),
    }


def normalize_style_payload(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    unknown = set(source) - set(STYLE_PAYLOAD_FIELDS)
    if unknown:
        raise ResourcePayloadError("风格资源包含不允许的字段")
    result: dict[str, Any] = {}
    for field in STYLE_PAYLOAD_FIELDS:
        default = [] if field in {"syntax", "vocabulary", "narrative_techniques", "examples"} else ""
        raw = source.get(field, default)
        result[field] = (
            _clean_list(raw, max_items=12, max_length=300)
            if isinstance(default, list)
            else _clean_text(raw, max_length=500)
        )
    if not any(result.values()):
        raise ResourcePayloadError("风格资源至少需要一项受控风格描述")
    return result


def normalize_quality_policy_payload(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    unknown = set(source) - {"gate_threshold", "regression_threshold"}
    if unknown:
        raise ResourcePayloadError("质量策略包含不允许的字段")
    try:
        gate_threshold = float(source.get("gate_threshold", 70.0))
        regression_threshold = float(source.get("regression_threshold", 3.0))
    except (TypeError, ValueError) as exc:
        raise ResourcePayloadError("质量阈值必须是数字") from exc
    if not 0 <= gate_threshold <= 100 or not 0 <= regression_threshold <= 100:
        raise ResourcePayloadError("质量阈值必须在 0 到 100 之间")
    return {
        "gate_threshold": gate_threshold,
        "regression_threshold": regression_threshold,
        "dimensions": list(QUALITY_DIMENSIONS),
    }


def normalize_resource_payload(kind: str, value: Any) -> dict[str, Any]:
    if kind == "content_types":
        return normalize_content_type_payload(value)
    if kind == "styles":
        return normalize_style_payload(value)
    if kind == "creative_templates":
        return normalize_creative_brief(value)
    if kind == "quality_policies":
        return normalize_quality_policy_payload(value)
    raise ResourcePayloadError("不支持的资源类型")


def system_style_resources(workspace_id: str) -> list[dict[str, Any]]:
    """返回当前工作区可见的系统内置风格，不允许原地修改。"""
    return [
        {
            "id": f"system_style_{key}",
            "workspace_id": workspace_id,
            "key": key,
            "name": str(profile.get("name", key))[:120],
            "description": "系统内置风格，只能复制到工作区后编辑",
            "status": "published",
            "version": 1,
            "is_system": True,
            "created_by": "system",
            "created_at": "",
            "updated_at": "",
            "payload": normalize_style_payload({field: profile.get(field) for field in STYLE_PAYLOAD_FIELDS}),
        }
        for key, profile in STYLE_PROFILES.items()
    ]


def system_style_resource(workspace_id: str, resource_id: str) -> dict[str, Any] | None:
    return next((item for item in system_style_resources(workspace_id) if item["id"] == resource_id), None)


def resource_snapshot(resource: dict[str, Any]) -> dict[str, Any]:
    """Copy only the immutable, prompt-safe parts of a resource into a novel."""
    return {
        "id": str(resource.get("id", "")),
        "key": str(resource.get("key", "")),
        "name": str(resource.get("name", "")),
        "description": str(resource.get("description", "")),
        "kind": str(resource.get("kind", "")),
        "version": int(resource.get("version", 1) or 1),
        "status": str(resource.get("status", "published")),
        "is_system": bool(resource.get("is_system", False)),
        "payload": dict(resource.get("payload") or {}),
    }


def default_content_type_snapshot() -> dict[str, Any]:
    return resource_snapshot(
        {
            "id": "system_content_type_default",
            "key": "default",
            "name": "通用小说",
            "description": "系统默认内容类型",
            "kind": "content_types",
            "version": 1,
            "status": "published",
            "is_system": True,
            "payload": normalize_content_type_payload({}),
        }
    )


def default_creative_template_snapshot(creative_brief: Any = None) -> dict[str, Any]:
    return resource_snapshot(
        {
            "id": "system_creative_template_default",
            "key": "default",
            "name": "默认创作约束",
            "description": "系统默认 CreativeBrief 模板",
            "kind": "creative_templates",
            "version": 1,
            "status": "published",
            "is_system": True,
            "payload": normalize_creative_brief(creative_brief),
        }
    )


def default_quality_policy_snapshot() -> dict[str, Any]:
    return resource_snapshot(
        {
            "id": "system_quality_policy_default",
            "key": "default",
            "name": "默认质量策略",
            "description": "系统默认质量门槛",
            "kind": "quality_policies",
            "version": 1,
            "status": "published",
            "is_system": True,
            "payload": normalize_quality_policy_payload({}),
        }
    )


def default_style_snapshot(workspace_id: str, style_key: str = "") -> dict[str, Any]:
    requested = str(style_key or "").strip()
    resource = system_style_resource(workspace_id, requested)
    if resource is None and requested:
        resource = next(
            (item for item in system_style_resources(workspace_id) if item.get("key") == requested),
            None,
        )
    if resource is None:
        resource = next(
            (item for item in system_style_resources(workspace_id) if item.get("key") == "jin_yong"),
            None,
        )
    result = resource_snapshot(
        resource
        or {
            "id": "system_style_default",
            "key": requested or "default",
            "name": requested or "默认风格",
            "kind": "styles",
            "version": 1,
            "status": "published",
            "is_system": True,
            "payload": {},
        }
    )
    # Preserve a legacy style label even when it predates the resource center.
    if requested and requested not in STYLE_PROFILES:
        result["legacy_key"] = requested
        result["key"] = requested
    return result


def default_resource_snapshots(
    workspace_id: str,
    *,
    style_key: str = "",
    creative_brief: Any = None,
) -> dict[str, dict[str, Any]]:
    return {
        "content_type_snapshot": default_content_type_snapshot(),
        "style_snapshot": default_style_snapshot(workspace_id, style_key),
        "creative_template_snapshot": default_creative_template_snapshot(creative_brief),
        "quality_policy_snapshot": default_quality_policy_snapshot(),
    }
