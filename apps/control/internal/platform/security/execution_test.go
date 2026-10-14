package security

import (
	"encoding/base64"
	"strings"
	"testing"

	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/governance"
)

type executionSnapshotWriterStub struct {
	key         string
	body        []byte
	contentType string
}

func TestExecutionCredentialEnvelopeIsEncryptedAndAuthenticated(t *testing.T) {
	api, _ := NewExecutionCipher("")
	key := "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
	var err error
	api, err = NewExecutionCipher(key)
	if err != nil {
		t.Fatal(err)
	}
	compatible, err := api.Decrypt("AAECAwQFBgcICQoLN3C5bayBp2mgMvLow4wMDbA9ISyez8BLVpsdh2sZiw==")
	if err != nil || compatible != "provider-secret" {
		t.Fatalf("Python-compatible credential envelope: %q %v", compatible, err)
	}
	envelope, err := api.Encrypt("provider-secret")
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(envelope, "provider-secret") {
		t.Fatal("provider secret was serialized in plaintext")
	}
	plaintext, err := api.Decrypt(envelope)
	if err != nil || plaintext != "provider-secret" {
		t.Fatalf("decrypt execution credential: %q %v", plaintext, err)
	}
	tampered, err := base64.StdEncoding.DecodeString(envelope)
	if err != nil {
		t.Fatal(err)
	}
	tampered[len(tampered)-1] ^= 1
	if _, err := api.Decrypt(base64.StdEncoding.EncodeToString(tampered)); err == nil {
		t.Fatal("tampered execution credential was accepted")
	}
}

func TestExecutionRouteSnapshotContainsOnlyCredentialReference(t *testing.T) {
	api, _ := NewExecutionCipher("")
	var err error
	api, err = NewExecutionCipher("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
	if err != nil {
		t.Fatal(err)
	}
	credentials := map[string]string{}
	route, err := governance.NewExecutionService(nil, api).RouteSnapshot(governance.FrozenRoute{
		ProfileVersionID: "profile-version-1",
		Provider:         "openai_compatible",
		ModelName:        "writer-v1",
		BaseURL:          "https://provider.example/v1",
		APIKey:           "provider-secret",
		MaxTokens:        4096,
		Fallback:         []governance.FrozenRoute{},
	}, credentials)
	if err != nil {
		t.Fatal(err)
	}
	if _, exists := route["api_key"]; exists {
		t.Fatal("plaintext API key was included in the execution snapshot")
	}
	if route["credential_ref"] != "profile-version-1" {
		t.Fatalf("credential reference = %v", route["credential_ref"])
	}
	if len(credentials) != 1 {
		t.Fatalf("credential envelope count = %d", len(credentials))
	}
	plaintext, err := api.Decrypt(credentials["profile-version-1"])
	if err != nil || plaintext != "provider-secret" {
		t.Fatalf("credential envelope did not preserve provider key: %q %v", plaintext, err)
	}
}
