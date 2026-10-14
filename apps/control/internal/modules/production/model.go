package production

import (
	"encoding/json"
	"time"
)

type Binding struct {
	ShotID              string `json:"shot_id"`
	VisualVersionID     string `json:"visual_version_id,omitempty"`
	VisualBlockPosition int    `json:"visual_block_position"`
	AudioVersionID      string `json:"audio_version_id,omitempty"`
	AudioBlockPosition  int    `json:"audio_block_position"`
}
type Request struct {
	ProjectID             string         `json:"project_id"`
	StoryboardVersionID   string         `json:"storyboard_version_id"`
	Title                 string         `json:"title"`
	IdempotencyKey        string         `json:"idempotency_key"`
	VisualMode            string         `json:"visual_mode"`
	Width                 int            `json:"width"`
	Height                int            `json:"height"`
	FPS                   int            `json:"fps"`
	Subtitles             bool           `json:"subtitles"`
	GenerateSpeech        bool           `json:"generate_speech"`
	GenerateMusic         bool           `json:"generate_music"`
	GenerateSoundEffects  bool           `json:"generate_sound_effects"`
	ImageParameters       map[string]any `json:"image_parameters,omitempty"`
	VideoParameters       map[string]any `json:"video_parameters,omitempty"`
	AudioParameters       map[string]any `json:"audio_parameters,omitempty"`
	MusicParameters       map[string]any `json:"music_parameters,omitempty"`
	SoundEffectParameters map[string]any `json:"sound_effect_parameters,omitempty"`
	Bindings              []Binding      `json:"bindings,omitempty"`
}
type Shot struct {
	ShotID         string   `json:"shot_id"`
	Description    string   `json:"description"`
	Dialogue       string   `json:"dialogue"`
	DurationMS     int      `json:"duration_ms"`
	SourceBlockIDs []string `json:"source_block_ids"`
}
type Child struct {
	Slot      string          `json:"slot"`
	Stage     string          `json:"stage"`
	ShotID    string          `json:"shot_id,omitempty"`
	JobID     string          `json:"job_id"`
	Status    string          `json:"status"`
	VersionID string          `json:"document_version_id,omitempty"`
	Result    json.RawMessage `json:"result"`
}
type Run struct {
	ID                string    `json:"run_id"`
	ProjectID         string    `json:"project_id"`
	Status            string    `json:"status"`
	Request           Request   `json:"request"`
	Children          []Child   `json:"children"`
	ErrorStage        string    `json:"error_stage,omitempty"`
	ErrorCode         string    `json:"error_code,omitempty"`
	TimelineVersionID string    `json:"timeline_version_id,omitempty"`
	FinalVersionID    string    `json:"final_version_id,omitempty"`
	CreatedAt         time.Time `json:"created_at"`
	UpdatedAt         time.Time `json:"updated_at"`
}
