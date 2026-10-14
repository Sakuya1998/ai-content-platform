package governance

type ModelTest struct {
	ModelName string `json:"model_name"`
	Kind      string `json:"kind"`
}

type ModelProfile struct {
	ID                string             `json:"id"`
	VersionID         string             `json:"version_id"`
	Version           int                `json:"version"`
	Name              string             `json:"name"`
	Provider          string             `json:"provider"`
	BaseURL           string             `json:"base_url"`
	APIKey            string             `json:"api_key,omitempty"`
	HasAPIKey         bool               `json:"has_api_key"`
	ChatModels        []string           `json:"chat_models"`
	EmbeddingModels   []string           `json:"embedding_models"`
	VideoModels       []VideoModel       `json:"video_models,omitempty"`
	ImageModels       []ImageModel       `json:"image_models,omitempty"`
	SpeechModels      []SpeechModel      `json:"speech_models,omitempty"`
	MusicModels       []MusicModel       `json:"music_models,omitempty"`
	SoundEffectModels []SoundEffectModel `json:"sound_effect_models,omitempty"`
	MaxTokens         int                `json:"max_tokens"`
	EncryptedKey      []byte             `json:"-"`
}
