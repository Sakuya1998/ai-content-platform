package governance

import (
	"bytes"
	"encoding/json"
	"errors"
	"time"
)

type ResourceVersion struct {
	ID          string          `json:"id"`
	VersionID   string          `json:"version_id"`
	WorkspaceID string          `json:"workspace_id"`
	Kind        string          `json:"kind"`
	Key         string          `json:"key"`
	Version     int             `json:"version"`
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Payload     json.RawMessage `json:"payload"`
	Status      string          `json:"status"`
	ContentHash string          `json:"content_hash"`
	CreatedAt   time.Time       `json:"created_at"`
}

type RouteTarget struct {
	ProfileID         string        `json:"profile_id"`
	ProfileVersionID  string        `json:"profile_version_id,omitempty"`
	ModelName         string        `json:"model_name"`
	FallbackProfileID string        `json:"fallback_profile_id,omitempty"`
	FallbackModelName string        `json:"fallback_model_name,omitempty"`
	Fallback          []RouteTarget `json:"fallback,omitempty"`
}
type FrozenRoute struct {
	ProfileVersionID        string                   `json:"profile_version_id"`
	Provider                string                   `json:"provider"`
	ModelName               string                   `json:"model_name"`
	BaseURL                 string                   `json:"base_url"`
	APIKey                  string                   `json:"api_key"`
	MaxTokens               int                      `json:"max_tokens"`
	Fallback                []FrozenRoute            `json:"fallback"`
	VideoCapabilities       *VideoCapabilities       `json:"video_capabilities,omitempty"`
	ImageCapabilities       *ImageCapabilities       `json:"image_capabilities,omitempty"`
	SpeechCapabilities      *SpeechCapabilities      `json:"speech_capabilities,omitempty"`
	MusicCapabilities       *MusicCapabilities       `json:"music_capabilities,omitempty"`
	SoundEffectCapabilities *SoundEffectCapabilities `json:"sound_effect_capabilities,omitempty"`
}
type FrozenRoutes struct {
	ID          string                 `json:"id"`
	Version     int                    `json:"version"`
	WorkspaceID string                 `json:"workspace_id"`
	Routes      map[string]FrozenRoute `json:"routes"`
}

type PromptMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}
type PromptVersion struct {
	ID              string          `json:"id"`
	VersionID       string          `json:"version_id"`
	WorkspaceID     *string         `json:"workspace_id"`
	Key             string          `json:"key"`
	NodeKey         string          `json:"node_key"`
	Name            string          `json:"name"`
	Description     string          `json:"description"`
	Version         int             `json:"version"`
	Messages        []PromptMessage `json:"messages"`
	VariablesSchema json.RawMessage `json:"variables_schema"`
	OutputSchema    json.RawMessage `json:"output_schema"`
	RendererVersion string          `json:"renderer_version"`
	Status          string          `json:"status"`
	ContentHash     string          `json:"content_hash"`
	CreatedAt       time.Time       `json:"created_at"`
}
type PromptPackEntry struct {
	NodeKey         string `json:"node_key"`
	PromptVersionID string `json:"prompt_version_id"`
}
type PromptPackVersion struct {
	ID          string            `json:"id"`
	VersionID   string            `json:"version_id"`
	WorkspaceID *string           `json:"workspace_id"`
	Key         string            `json:"key"`
	Name        string            `json:"name"`
	Description string            `json:"description"`
	Version     int               `json:"version"`
	Status      string            `json:"status"`
	ContentHash string            `json:"content_hash"`
	Entries     []PromptPackEntry `json:"entries"`
	CreatedAt   time.Time         `json:"created_at"`
}
type FrozenPromptBinding struct {
	PackVersionID string            `json:"pack_version_id"`
	Bindings      map[string]string `json:"bindings"`
}

type CreationPolicyVersion struct {
	ID          string          `json:"id"`
	VersionID   string          `json:"version_id"`
	WorkspaceID *string         `json:"workspace_id"`
	Key         string          `json:"key"`
	Name        string          `json:"name"`
	Version     int             `json:"version"`
	Status      string          `json:"status"`
	Payload     json.RawMessage `json:"payload"`
	ContentHash string          `json:"content_hash"`
	CreatedAt   time.Time       `json:"created_at"`
}

type VideoModel struct {
	ModelName    string            `json:"model_name"`
	Capabilities VideoCapabilities `json:"capabilities"`
}

type VideoCapabilities struct {
	TextToVideo   bool     `json:"text_to_video"`
	ImageToVideo  bool     `json:"image_to_video"`
	Durations     []int    `json:"durations"`
	Ratios        []string `json:"ratios"`
	Resolutions   []string `json:"resolutions"`
	GenerateAudio bool     `json:"generate_audio"`
	Cancellation  bool     `json:"cancellation"`
}

type ImageModel struct {
	ModelName    string            `json:"model_name"`
	Capabilities ImageCapabilities `json:"capabilities"`
}
type ImageCapabilities struct {
	Generation bool     `json:"generation"`
	Editing    bool     `json:"editing"`
	Sizes      []string `json:"sizes"`
	Formats    []string `json:"formats"`
	MaxImages  int      `json:"max_images"`
}
type SpeechModel struct {
	ModelName    string             `json:"model_name"`
	Capabilities SpeechCapabilities `json:"capabilities"`
}
type SpeechCapabilities struct {
	Voices        []string `json:"voices"`
	Formats       []string `json:"formats"`
	Languages     []string `json:"languages"`
	MinSpeed      float64  `json:"min_speed"`
	MaxSpeed      float64  `json:"max_speed"`
	MaxInputChars int      `json:"max_input_chars"`
	Instructions  bool     `json:"instructions"`
}

func decodeMediaCapabilities(data []byte, target any, required []string) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for _, field := range required {
		value, ok := fields[field]
		if !ok || bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return errors.New("media capability field required: " + field)
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	return decoder.Decode(target)
}
func (c *ImageCapabilities) UnmarshalJSON(data []byte) error {
	type plain ImageCapabilities
	return decodeMediaCapabilities(data, (*plain)(c), []string{"generation", "editing", "sizes", "formats", "max_images"})
}
func (c *SpeechCapabilities) UnmarshalJSON(data []byte) error {
	type plain SpeechCapabilities
	return decodeMediaCapabilities(data, (*plain)(c), []string{"voices", "formats", "languages", "min_speed", "max_speed", "max_input_chars", "instructions"})
}

type MusicModel struct {
	ModelName    string            `json:"model_name"`
	Capabilities MusicCapabilities `json:"capabilities"`
}

type MusicCapabilities struct {
	OutputFormats []string `json:"output_formats"`
	MinDurationMS int      `json:"min_duration_ms"`
	MaxDurationMS int      `json:"max_duration_ms"`
	Instrumental  bool     `json:"instrumental"`
}

type SoundEffectModel struct {
	ModelName    string                  `json:"model_name"`
	Capabilities SoundEffectCapabilities `json:"capabilities"`
}

type SoundEffectCapabilities struct {
	OutputFormats      []string `json:"output_formats"`
	MinDurationSeconds float64  `json:"min_duration_seconds"`
	MaxDurationSeconds float64  `json:"max_duration_seconds"`
	PromptInfluence    bool     `json:"prompt_influence"`
	Loop               bool     `json:"loop"`
}

func (c *MusicCapabilities) UnmarshalJSON(data []byte) error {
	type plain MusicCapabilities
	return decodeMediaCapabilities(data, (*plain)(c), []string{"output_formats", "min_duration_ms", "max_duration_ms", "instrumental"})
}

func (c *SoundEffectCapabilities) UnmarshalJSON(data []byte) error {
	type plain SoundEffectCapabilities
	return decodeMediaCapabilities(data, (*plain)(c), []string{"output_formats", "min_duration_seconds", "max_duration_seconds", "prompt_influence", "loop"})
}
