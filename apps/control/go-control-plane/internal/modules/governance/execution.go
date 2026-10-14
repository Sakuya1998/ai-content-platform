package governance

import (
	"context"
	"errors"
	"strings"
)

type ExecutionCatalog interface {
	Prompt(context.Context, string, string) (PromptVersion, error)
	Policy(context.Context, string, string) (CreationPolicyVersion, error)
	Routes(context.Context, string, string) (FrozenRoutes, error)
	Resource(context.Context, string, string) (ResourceVersion, error)
}
type CredentialEncryptor interface{ Encrypt(string) (string, error) }
type ExecutionService struct {
	catalog   ExecutionCatalog
	encryptor CredentialEncryptor
}

func NewExecutionService(catalog ExecutionCatalog, encryptor CredentialEncryptor) *ExecutionService {
	return &ExecutionService{catalog: catalog, encryptor: encryptor}
}

func executionStringMap(value any) (map[string]string, error) {
	switch values := value.(type) {
	case map[string]string:
		return values, nil
	case map[string]any:
		result := make(map[string]string, len(values))
		for key, item := range values {
			text, ok := item.(string)
			if !ok {
				return nil, errors.New("execution snapshot reference must be a string")
			}
			result[key] = text
		}
		return result, nil
	default:
		return nil, errors.New("execution snapshot references are required")
	}
}

func executionStringSlice(value any) ([]string, error) {
	switch values := value.(type) {
	case []string:
		return values, nil
	case []any:
		result := make([]string, 0, len(values))
		for _, item := range values {
			text, ok := item.(string)
			if !ok {
				return nil, errors.New("execution snapshot resource reference must be a string")
			}
			result = append(result, text)
		}
		return result, nil
	default:
		return []string{}, nil
	}
}

func (g *ExecutionService) RouteSnapshot(route FrozenRoute, credentials map[string]string) (map[string]any, error) {
	result := map[string]any{
		"profile_version_id": route.ProfileVersionID,
		"provider":           route.Provider,
		"model_name":         route.ModelName,
		"base_url":           route.BaseURL,
		"max_tokens":         route.MaxTokens,
	}
	if route.APIKey != "" {
		if route.ProfileVersionID == "" {
			return nil, errors.New("frozen model route has no credential identity")
		}
		if _, exists := credentials[route.ProfileVersionID]; !exists {
			envelope, err := g.encryptor.Encrypt(route.APIKey)
			if err != nil {
				return nil, err
			}
			credentials[route.ProfileVersionID] = envelope
		}
		result["credential_ref"] = route.ProfileVersionID
	}
	if route.VideoCapabilities != nil {
		result["video_capabilities"] = route.VideoCapabilities
	}
	if route.ImageCapabilities != nil {
		result["image_capabilities"] = route.ImageCapabilities
	}
	if route.SpeechCapabilities != nil {
		result["speech_capabilities"] = route.SpeechCapabilities
	}
	if route.MusicCapabilities != nil {
		result["music_capabilities"] = route.MusicCapabilities
	}
	if route.SoundEffectCapabilities != nil {
		result["sound_effect_capabilities"] = route.SoundEffectCapabilities
	}
	fallbacks := make([]any, 0, len(route.Fallback))
	for _, fallback := range route.Fallback {
		value, err := g.RouteSnapshot(fallback, credentials)
		if err != nil {
			return nil, err
		}
		fallbacks = append(fallbacks, value)
	}
	result["fallback"] = fallbacks
	return result, nil
}

func (g *ExecutionService) Resolve(ctx context.Context, workspace string, snapshot map[string]any) (map[string]any, error) {
	resources := map[string]any{
		"prompts":         map[string]any{},
		"prompt_pack_id":  "",
		"creation_policy": nil,
		"model_routes":    map[string]any{},
		"credentials":     map[string]string{},
		"governance":      map[string]any{},
	}
	prompts := resources["prompts"].(map[string]any)
	if g.catalog == nil {
		return nil, errors.New("prompt catalog must be configured before snapshot resolution")
	}
	promptBindings, err := executionStringMap(snapshot["prompt_bindings"])
	if err != nil || len(promptBindings) == 0 {
		return nil, errors.New("frozen Prompt bindings are required")
	}
	for node, versionID := range promptBindings {
		prompt, err := g.catalog.Prompt(ctx, workspace, versionID)
		if err != nil {
			return nil, err
		}
		if prompt.NodeKey != node || prompt.VersionID != versionID {
			return nil, errors.New("frozen Prompt identity conflict")
		}
		prompts[node] = prompt
	}
	if packs, err := executionStringSlice(snapshot["prompt_packs"]); err != nil || len(packs) != 1 {
		return nil, errors.New("one frozen Prompt Pack version is required")
	} else {
		resources["prompt_pack_id"] = packs[0]
	}
	if g.catalog == nil {
		return nil, errors.New("creation policy catalog must be configured before snapshot resolution")
	}
	policyRef, err := executionStringMap(snapshot["creation_policy"])
	if err != nil || policyRef["version_id"] == "" || policyRef["content_hash"] == "" {
		return nil, errors.New("frozen creation policy reference is required")
	}
	policy, err := g.catalog.Policy(ctx, workspace, policyRef["version_id"])
	if err != nil {
		return nil, err
	}
	if policy.ContentHash != policyRef["content_hash"] {
		return nil, errors.New("frozen creation policy checksum conflict")
	}
	resources["creation_policy"] = policy

	routeRefs, err := executionStringMap(snapshot["model_routes"])
	if err != nil {
		return nil, err
	}
	resolvedRoutes := resources["model_routes"].(map[string]any)
	routeCache := map[string]FrozenRoutes{}
	credentials := resources["credentials"].(map[string]string)
	for purpose, routeID := range routeRefs {
		if strings.HasPrefix(routeID, "runtime-") {
			continue
		}
		frozen, exists := routeCache[routeID]
		if !exists {
			frozen, err = g.catalog.Routes(ctx, workspace, routeID)
			if err != nil {
				return nil, err
			}
			routeCache[routeID] = frozen
		}
		route, ok := frozen.Routes[purpose]
		if !ok {
			return nil, errors.New("frozen model route purpose is missing")
		}
		resolvedRoutes[purpose], err = g.RouteSnapshot(route, credentials)
		if err != nil {
			return nil, err
		}
		resolvedRoutes[purpose].(map[string]any)["route_version_id"] = routeID
	}

	governance := resources["governance"].(map[string]any)
	for kind, field := range map[string]string{"skill": "skills", "tool": "tools", "mcp_policy": "mcp_policies"} {
		references, err := executionStringSlice(snapshot[field])
		if err != nil {
			return nil, err
		}
		for _, versionID := range references {
			if strings.Contains(versionID, "@") {
				continue
			}
			resource, err := g.catalog.Resource(ctx, workspace, versionID)
			if err != nil {
				return nil, err
			}
			if resource.Kind != kind || resource.Status != "published" {
				return nil, errors.New("frozen governance resource identity conflict")
			}
			governance[versionID] = resource
		}
	}
	return resources, nil
}
