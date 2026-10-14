package governance

import (
	"context"
	"testing"
)

type executionCatalogStub struct{}

func (executionCatalogStub) Prompt(context.Context, string, string) (PromptVersion, error) {
	return PromptVersion{NodeKey: "write", VersionID: "prompt-1"}, nil
}
func (executionCatalogStub) Policy(context.Context, string, string) (CreationPolicyVersion, error) {
	return CreationPolicyVersion{ContentHash: "policy-hash"}, nil
}
func (executionCatalogStub) Routes(context.Context, string, string) (FrozenRoutes, error) {
	return FrozenRoutes{Routes: map[string]FrozenRoute{"creative": {ProfileVersionID: "profile-1", APIKey: "secret"}}}, nil
}
func (executionCatalogStub) Resource(context.Context, string, string) (ResourceVersion, error) {
	return ResourceVersion{}, nil
}

type executionEncryptorStub struct{}

func (executionEncryptorStub) Encrypt(secret string) (string, error) { return "sealed:" + secret, nil }

func TestExecutionServiceResolvesFrozenReferencesAndSealsCredentials(t *testing.T) {
	service := NewExecutionService(executionCatalogStub{}, executionEncryptorStub{})
	result, err := service.Resolve(context.Background(), "workspace", map[string]any{"prompt_bindings": map[string]string{"write": "prompt-1"}, "prompt_packs": []string{"pack-1"}, "creation_policy": map[string]string{"version_id": "policy-1", "content_hash": "policy-hash"}, "model_routes": map[string]string{"creative": "route-1"}})
	if err != nil {
		t.Fatal(err)
	}
	route := result["model_routes"].(map[string]any)["creative"].(map[string]any)
	if _, ok := route["api_key"]; ok {
		t.Fatal("plaintext secret leaked")
	}
	if route["credential_ref"] != "profile-1" || result["credentials"].(map[string]string)["profile-1"] != "sealed:secret" {
		t.Fatal("credential resolution failed")
	}
}
