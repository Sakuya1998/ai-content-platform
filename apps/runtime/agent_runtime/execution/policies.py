"""Access only the creation policy version frozen into the current task."""

from agent_runtime.execution.context import current_execution


def current_creation_policy(*, required=False):
    context = current_execution(required=False)
    version = context.creation_policy if context else None
    if version is None and required:
        raise ValueError("missing frozen creation policy")
    return version.payload if version else None


def policy_provenance():
    context = current_execution(required=False)
    if context and context.creation_policy:
        return {
            "creation_policy_version_id": context.creation_policy.version_id,
            "creation_policy_hash": context.creation_policy.content_hash,
        }
    return {}
