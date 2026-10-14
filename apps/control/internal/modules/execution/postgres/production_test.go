package postgres

import (
	"github.com/google/uuid"
	"testing"
)

func TestProductionBoundsAndAssembleContract(t *testing.T) {
	r := ProductionRequest{ProjectID: uuid.NewString(), StoryboardVersionID: uuid.NewString(), Title: "Film", IdempotencyKey: "film", VisualMode: "image", Width: 1280, Height: 720, FPS: 24}
	if !validProduction(r) {
		t.Fatal("valid production rejected")
	}
	r.Width = 1279
	if validProduction(r) {
		t.Fatal("odd frame dimension accepted")
	}
	shot := map[string]any{"shot_id": "shot1", "visual_version_id": uuid.NewString(), "visual_block_position": 0}
	args := map[string]any{"assemble": map[string]any{"width": 1280, "height": 720, "fps": 24, "subtitles": true, "shots": []any{shot}}}
	if !validAssembleArguments(args) {
		t.Fatal("valid assembly rejected")
	}
	shot["arbitrary"] = true
	if validAssembleArguments(args) {
		t.Fatal("unknown assembly field accepted")
	}
}

func TestProductionMusicAndSoundEffectArgumentsAreBounded(t *testing.T) {
	r := ProductionRequest{
		ProjectID: uuid.NewString(), StoryboardVersionID: uuid.NewString(), Title: "Film", IdempotencyKey: "film",
		VisualMode: "image", Width: 1280, Height: 720, FPS: 24,
		GenerateMusic: true, GenerateSoundEffects: true,
		MusicParameters:       map[string]any{"output_format": "mp3_44100_128", "force_instrumental": true},
		SoundEffectParameters: map[string]any{"output_format": "mp3_22050_32", "prompt_influence": 0.5, "loop": false},
	}
	if !validProduction(r) {
		t.Fatal("valid music and sound effect parameters rejected")
	}
	r.MusicParameters["output_format"] = "wav"
	if validProduction(r) {
		t.Fatal("unsupported music output format accepted")
	}
	r.MusicParameters["output_format"] = "mp3_44100_128"
	r.SoundEffectParameters["duration_seconds"] = 30.1
	if validProduction(r) {
		t.Fatal("out-of-range sound effect duration accepted")
	}
}

func TestAssembleContractAcceptsBackgroundMusicAndShotEffects(t *testing.T) {
	shotID := "shot1"
	args := map[string]any{"assemble": map[string]any{
		"width": 1280, "height": 720, "fps": 24, "subtitles": true,
		"shots":            []any{map[string]any{"shot_id": shotID, "visual_version_id": uuid.NewString(), "visual_block_position": 0}},
		"music_version_id": uuid.NewString(), "music_block_position": 0,
		"effects": []any{map[string]any{"shot_id": shotID, "version_id": uuid.NewString(), "block_position": 0}},
	}}
	if !validAssembleArguments(args) {
		t.Fatal("music and effect tracks rejected by assembly contract")
	}
	args["assemble"].(map[string]any)["effects"] = []any{map[string]any{"shot_id": shotID, "version_id": uuid.NewString(), "block_position": -1}}
	if validAssembleArguments(args) {
		t.Fatal("negative sound effect block position accepted")
	}
}
