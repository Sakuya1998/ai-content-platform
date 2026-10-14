"""Resolve the immutable capabilities captured by a submitted task."""

from agent_runtime.execution.context import current_execution


async def bind_execution_models(snapshot, workspace_id, *, resolved_resources=None):
    context = current_execution(required=False)
    if context is None:
        return
    if resolved_resources is None:
        raise ValueError("resolved immutable execution resources are required")
    bind_snapshot_resources(context, snapshot, workspace_id, resolved_resources)


def bind_snapshot_resources(context, snapshot, workspace_id, resources):
    from agent_runtime.domain.policies import CreationPolicyVersion
    from agent_runtime.execution.secrets import decrypt_execution_credential
    from agent_runtime.prompts.renderer import PromptVersion

    policy = None
    if snapshot.creation_policy is not None:
        policy = CreationPolicyVersion.model_validate(resources.get("creation_policy"))
        reference = snapshot.creation_policy
        if policy.version_id != reference.get("version_id") or policy.content_hash != reference.get("content_hash"):
            raise ValueError("frozen creation policy binding conflict")
    if not snapshot.prompt_bindings or len(snapshot.prompt_packs) != 1:
        raise ValueError("complete frozen Prompt bindings are required")
    if resources.get("prompt_pack_id") != snapshot.prompt_packs[0]:
        raise ValueError("frozen Prompt Pack identity conflict")
    prompts = {}
    for node, version_id in snapshot.prompt_bindings.items():
        prompt = PromptVersion.model_validate(resources.get("prompts", {}).get(node))
        if prompt.node_key != node or prompt.version_id != version_id:
            raise ValueError("frozen Prompt identity conflict")
        prompts[node] = prompt

    def route_with_secret(route):
        if not isinstance(route, dict) or not route.get("provider") or not route.get("model_name"):
            raise ValueError("invalid frozen model route")
        if "api_key" in route:
            raise ValueError("plaintext model credential in execution snapshot")
        value = dict(route)
        if reference := route.get("credential_ref"):
            envelope = resources.get("credentials", {}).get(reference)
            if not envelope or reference != route.get("profile_version_id"):
                raise ValueError("frozen model credential identity conflict")
            value["api_key"] = decrypt_execution_credential(envelope, context.settings.execution_encryption_key)
        value["fallback"] = [route_with_secret(item) for item in route.get("fallback", [])]
        return value

    routes = {}
    for purpose, version_id in snapshot.model_routes.items():
        if version_id == f"runtime-{purpose}@1":
            continue
        route = resources.get("model_routes", {}).get(purpose)
        if not isinstance(route, dict) or route.get("route_version_id") != version_id:
            raise ValueError("frozen model route version conflict")
        routes[purpose] = route_with_secret(route)
    governance = {}
    for kind, references in [
        ("skill", snapshot.skills),
        ("tool", snapshot.tools),
        ("mcp_policy", snapshot.mcp_policies),
    ]:
        for version_id in references:
            if "@" in version_id:
                continue
            resource = resources.get("governance", {}).get(version_id)
            if (
                not isinstance(resource, dict)
                or resource.get("version_id") != version_id
                or resource.get("kind") != kind
                or resource.get("status") != "published"
                or resource.get("workspace_id") != workspace_id
            ):
                raise ValueError("frozen governance resource identity conflict")
            governance[version_id] = resource
    context.creation_policy = policy
    context.prompt_versions = prompts
    context.prompt_pack_versions = dict.fromkeys(prompts, snapshot.prompt_packs[0])
    context.model_routes = routes
    context.resource_versions = governance
