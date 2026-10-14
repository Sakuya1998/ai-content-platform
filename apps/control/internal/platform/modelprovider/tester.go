package modelprovider

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
)

type ModelTester struct{ client *http.Client }

func NewModelTester() *ModelTester {
	return &ModelTester{client: &http.Client{Timeout: 20 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}
}

func (g *ModelTester) Test(ctx context.Context, p governance.ModelProfile, input governance.ModelTest) (map[string]any, error) {
	models := p.ChatModels
	if input.Kind == "embedding" {
		models = p.EmbeddingModels
	} else if input.Kind != "chat" {
		return nil, errors.New("kind must be chat or embedding")
	}
	if !governance.ModelContains(models, input.ModelName) {
		return nil, errors.New("model not present in profile")
	}
	secret := p.APIKey
	base := p.BaseURL
	if base == "" {
		base, _ = governance.ProviderURL(p.Provider)
	}
	path := "/chat/completions"
	payload := map[string]any{"model": input.ModelName, "messages": []any{map[string]string{"role": "user", "content": "Reply OK"}}, "max_tokens": 8}
	if input.Kind == "embedding" {
		path = "/embeddings"
		payload = map[string]any{"model": input.ModelName, "input": "connection test"}
	}
	if p.Provider == "anthropic" {
		if input.Kind != "chat" {
			return nil, errors.New("anthropic embeddings unsupported")
		}
		path = "/v1/messages"
	}
	data, _ := json.Marshal(payload)
	req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(base, "/")+path, strings.NewReader(string(data)))
	if err != nil {
		return nil, errors.New("invalid model URL")
	}
	req.Header.Set("Content-Type", "application/json")
	if secret != "" {
		req.Header.Set("Authorization", "Bearer "+secret)
	}
	if p.Provider == "anthropic" {
		req.Header.Set("x-api-key", secret)
		req.Header.Set("anthropic-version", "2023-06-01")
	}
	response, err := g.client.Do(req)
	if err != nil {
		return nil, errors.New("model connection failed")
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	if err != nil || response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, errors.New("model provider rejected connection test")
	}
	var result map[string]any
	if json.Unmarshal(body, &result) != nil {
		return nil, errors.New("invalid model provider response")
	}
	valid := false
	if input.Kind == "embedding" {
		_, valid = result["data"]
	} else {
		_, valid = result["choices"]
		if p.Provider == "anthropic" {
			_, valid = result["content"]
		}
	}
	if !valid {
		return nil, errors.New("model provider response missing result")
	}
	return map[string]any{"ok": true, "profile_id": p.ID, "model_name": input.ModelName, "kind": input.Kind}, nil
}
