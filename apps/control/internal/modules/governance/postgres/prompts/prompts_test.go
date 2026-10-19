package prompts

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
)

func TestPromptBodyDecoderRejectsTrailingJSON(t *testing.T) {
	request := &catalogRequest{payload: json.RawMessage(`{"key":"valid"} {}`)}
	var input struct {
		Key string `json:"key"`
	}
	if err := decodeCatalogBody(request, &input); err == nil {
		t.Fatal("trailing JSON values must be rejected")
	}
}

func TestRepositoryWithoutStorageReturnsError(t *testing.T) {
	repo := NewRepository(nil, nil)
	_, err := repo.ExecutePrompt(context.Background(), Principal{}, "prompts", governance.CatalogCommand{Action: governance.Read})
	if err == nil {
		t.Fatal("missing storage must return an error")
	}
}
