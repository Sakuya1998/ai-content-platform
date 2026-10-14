package governance

import (
	"encoding/json"
	"errors"
	"net/url"
	"strings"
)

func NormalizeKind(kind string) string {
	kind = strings.ReplaceAll(kind, "-", "_")
	switch kind {
	case "content_types", "styles", "creative_templates", "quality_policies", "prompt_pack", "skill", "tool", "mcp_policy":
		return kind
	case "prompts", "prompt_packs":
		return "prompt_pack"
	case "skills":
		return "skill"
	case "tools":
		return "tool"
	case "mcp", "mcp_policies":
		return "mcp_policy"
	}
	return ""
}

func validateResourcePayload(kind string, payload json.RawMessage) error {
	var obj map[string]any
	if json.Unmarshal(payload, &obj) != nil || obj == nil {
		return errors.New("payload must be an object")
	}
	if kind == "prompt_pack" {
		if text, ok := obj["template"].(string); ok && strings.TrimSpace(text) == "" {
			return errors.New("prompt template is empty")
		}
	}
	if kind == "tool" {
		if method, ok := obj["method"].(string); ok && method != "GET" && method != "POST" {
			return errors.New("unsupported tool HTTP method")
		}
	}
	if kind == "mcp_policy" {
		if endpoint, ok := obj["url"].(string); ok {
			u, err := url.Parse(endpoint)
			if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
				return errors.New("invalid MCP URL")
			}
		}
	}
	return nil
}

func regexpResourceKey(key string) bool {
	if len(key) < 1 || len(key) > 80 {
		return false
	}
	for _, c := range key {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '-' || c == '.') {
			return false
		}
	}
	return true
}

type ModelProfileWrite struct {
	Name              string             `json:"name"`
	Provider          string             `json:"provider"`
	BaseURL           string             `json:"base_url"`
	APIKey            string             `json:"api_key"`
	ClearAPIKey       bool               `json:"clear_api_key"`
	ChatModels        []string           `json:"chat_models"`
	EmbeddingModels   []string           `json:"embedding_models"`
	VideoModels       []VideoModel       `json:"video_models,omitempty"`
	ImageModels       []ImageModel       `json:"image_models,omitempty"`
	SpeechModels      []SpeechModel      `json:"speech_models,omitempty"`
	MusicModels       []MusicModel       `json:"music_models,omitempty"`
	SoundEffectModels []SoundEffectModel `json:"sound_effect_models,omitempty"`
	MaxTokens         int                `json:"max_tokens"`
}

func providerURL(provider string) (string, bool) {
	switch provider {
	case "openai":
		return "https://api.openai.com/v1", true
	case "anthropic":
		return "https://api.anthropic.com", true
	case "deepseek":
		return "https://api.deepseek.com", true
	case "gemini":
		return "https://generativelanguage.googleapis.com/v1beta/openai", true
	case "qwen":
		return "https://dashscope.aliyuncs.com/compatible-mode/v1", true
	case "xai":
		return "https://api.x.ai/v1", true
	case "groq":
		return "https://api.groq.com/openai/v1", true
	case "moonshot":
		return "https://api.moonshot.cn/v1", true
	case "zhipu":
		return "https://open.bigmodel.cn/api/paas/v4", true
	case "volcengine_tts":
		return "https://openspeech.bytedance.com", true
	case "elevenlabs":
		return "https://api.elevenlabs.io/v1", true
	case "volcengine":
		return "https://ark.cn-beijing.volces.com/api/v3", true
	case "siliconflow":
		return "https://api.siliconflow.cn/v1", true
	case "openrouter":
		return "https://openrouter.ai/api/v1", true
	case "openai_compatible", "openai-compatible", "ollama":
		return "", true
	}
	return "", false
}

func validateModelProfile(in ModelProfileWrite) error {
	if len(strings.TrimSpace(in.Name)) == 0 || len(in.Name) > 80 {
		return errors.New("profile name required")
	}
	_, ok := providerURL(in.Provider)
	if !ok {
		return errors.New("unsupported model provider")
	}
	if in.BaseURL != "" {
		u, err := url.Parse(in.BaseURL)
		if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
			return errors.New("invalid model base URL")
		}
	} else if in.Provider == "openai_compatible" || in.Provider == "openai-compatible" || in.Provider == "ollama" {
		return errors.New("model base URL required")
	}
	if len(in.ChatModels) > 50 || len(in.EmbeddingModels) > 50 {
		return errors.New("too many models")
	}
	for _, name := range append(append([]string{}, in.ChatModels...), in.EmbeddingModels...) {
		if strings.TrimSpace(name) == "" || len(name) > 200 {
			return errors.New("invalid model name")
		}
	}
	if err := validateVideoModels(in.VideoModels); err != nil {
		return err
	}
	if err := validateImageModels(in.ImageModels); err != nil {
		return err
	}
	if err := validateSpeechModels(in.SpeechModels); err != nil {
		return err
	}
	if err := validateMusicModels(in.MusicModels); err != nil {
		return err
	}
	if (len(in.MusicModels) > 0 || len(in.SoundEffectModels) > 0) && in.Provider != "elevenlabs" {
		return errors.New("music and sound effect models require the elevenlabs provider")
	}
	return validateSoundEffectModels(in.SoundEffectModels)
}

func routeUsesProfile(target RouteTarget, id string) bool {
	if target.ProfileID == id {
		return true
	}
	for _, fallback := range target.Fallback {
		if routeUsesProfile(fallback, id) {
			return true
		}
	}
	return false
}

var ValidateResourcePayload = validateResourcePayload

var RegexpResourceKey = regexpResourceKey

var ProviderURL = providerURL

var ValidateModelProfile = validateModelProfile

var RouteUsesProfile = routeUsesProfile
