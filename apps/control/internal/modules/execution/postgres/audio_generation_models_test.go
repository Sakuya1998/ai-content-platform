package postgres

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	"github.com/google/uuid"
)

const audioGenerationProfileJSON = `{"name":"ElevenLabs","provider":"elevenlabs","api_key":"media-key","chat_models":["chat"],"embedding_models":["embed"],"music_models":[{"model_name":"music_v2_5","capabilities":{"output_formats":["mp3_44100_128","mp3_48000_192"],"min_duration_ms":3000,"max_duration_ms":600000,"instrumental":true}}],"sound_effect_models":[{"model_name":"eleven_text_to_sound_v2","capabilities":{"output_formats":["mp3_22050_32","mp3_44100_128"],"min_duration_seconds":0.5,"max_duration_seconds":30,"prompt_influence":true,"loop":true}}]}`

func TestAudioGenerationProfileCapabilitiesAreValidatedAndPreserved(t *testing.T) {
	var input modelProfileWrite
	if err := json.Unmarshal([]byte(audioGenerationProfileJSON), &input); err != nil {
		t.Fatal(err)
	}
	if err := validateModelProfile(input); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(input)
	if err != nil {
		t.Fatal(err)
	}
	var profile map[string]any
	if err = json.Unmarshal(raw, &profile); err != nil {
		t.Fatal(err)
	}
	for _, field := range []string{"music_models", "sound_effect_models"} {
		if values, ok := profile[field].([]any); !ok || len(values) != 1 {
			t.Fatalf("discarded %s: %s", field, raw)
		}
	}
	for _, replacement := range [][2]string{
		{`"min_duration_ms":3000`, `"min_duration_ms":2000`},
		{`"max_duration_ms":600000`, `"max_duration_ms":600001`},
		{`"instrumental":true`, `"instrumental":"true"`},
		{`"output_formats":["mp3_44100_128","mp3_48000_192"]`, `"output_formats":["wav"]`},
		{`"min_duration_seconds":0.5`, `"min_duration_seconds":0.1`},
		{`"max_duration_seconds":30`, `"max_duration_seconds":31`},
		{`"prompt_influence":true`, `"prompt_influence":null`},
		{`"model_name":"music_v2_5"`, `"model_name":"other"`},
		{`"provider":"elevenlabs"`, `"provider":"openai"`},
	} {
		bad := strings.Replace(audioGenerationProfileJSON, replacement[0], replacement[1], 1)
		var candidate modelProfileWrite
		if err := json.Unmarshal([]byte(bad), &candidate); err == nil && validateModelProfile(candidate) == nil {
			t.Fatalf("invalid model capability accepted: %s", bad)
		}
	}
	if base, ok := providerURL("elevenlabs"); !ok || base != "https://api.elevenlabs.io/v1" {
		t.Fatalf("ElevenLabs default endpoint: %q %v", base, ok)
	}
}

func TestMusicAndSoundEffectGenerationRequestBounds(t *testing.T) {
	for _, kind := range []string{"music", "effect"} {
		base := GenerationRequest{ProjectID: uuid.NewString(), DocumentType: "audio", Title: "Audio", Objective: "Create audio", Arguments: map[string]any{kind: map[string]any{}}}
		if !validGeneration(base) {
			t.Fatalf("%s defaults rejected", kind)
		}
		base.Nodes = []string{"plan", "generate", "review"}
		if !validGeneration(base) {
			t.Fatalf("%s controlled nodes rejected", kind)
		}
		base.Arguments = map[string]any{"audio": map[string]any{}, kind: map[string]any{}}
		if validGeneration(base) {
			t.Fatalf("mixed speech and %s arguments accepted", kind)
		}
	}
	for _, args := range []map[string]any{
		{"music": map[string]any{"output_format": "mp3_44100_128", "music_length_ms": json.Number("12000"), "force_instrumental": true}},
		{"effect": map[string]any{"output_format": "mp3_22050_32", "duration_seconds": 2.5, "prompt_influence": 0.8, "loop": false}},
	} {
		request := GenerationRequest{ProjectID: uuid.NewString(), DocumentType: "audio", Title: "Audio", Objective: "Create audio", Arguments: args}
		if !validGeneration(request) {
			t.Fatalf("valid generated audio request rejected: %#v", args)
		}
	}
	for _, args := range []map[string]any{
		{"music": map[string]any{"output_format": "wav"}},
		{"music": map[string]any{"music_length_ms": 2999}},
		{"music": map[string]any{"force_instrumental": "yes"}},
		{"effect": map[string]any{"output_format": "audio/mpeg"}},
		{"effect": map[string]any{"duration_seconds": 30.1}},
		{"effect": map[string]any{"prompt_influence": 1.1}},
		{"effect": map[string]any{"loop": "false"}},
	} {
		request := GenerationRequest{ProjectID: uuid.NewString(), DocumentType: "audio", Title: "Audio", Objective: "Create audio", Arguments: args}
		if validGeneration(request) {
			t.Fatalf("invalid generated audio request accepted: %#v", args)
		}
	}
}

func TestMusicAndSoundEffectSnapshotsUseSeparateFrozenRoutesAndPrompts(t *testing.T) {
	for _, item := range []struct {
		argument, workflow, purpose, skill, node string
	}{
		{"music", "media.music@1", "music_generation", "controlled-music@1", "media.music.plan"},
		{"effect", "media.sound_effect@1", "sound_effect_generation", "controlled-sound-effect@1", "media.sound_effect.plan"},
	} {
		request := GenerationRequest{
			ProjectID: uuid.NewString(), DocumentType: "audio", Title: "Audio", Objective: "Create audio",
			Arguments: map[string]any{item.argument: map[string]any{}},
		}
		if !validGeneration(request) || generatedAudioWorkflow(request.Arguments) != item.workflow {
			t.Fatalf("%s did not select its workflow", item.argument)
		}
		snapshot := generationSnapshot(request, generatedAudioWorkflow(request.Arguments))
		if snapshot["skills"].([]string)[0] != item.skill || snapshot["model_routes"].(map[string]string)[item.purpose] != "current" {
			t.Fatalf("%s snapshot did not freeze the route and skill: %#v", item.argument, snapshot)
		}
		nodes, err := workflowPromptNodes(item.workflow)
		if err != nil || nodes[0] != item.node || len(nodes) != 3 {
			t.Fatalf("%s prompt nodes: %v %v", item.argument, nodes, err)
		}
	}
}

func TestAudioGenerationRoutesFreezeSeparateElevenLabsCapabilities(t *testing.T) {
	pool := identityTestPool(t)
	identity := NewIdentityAPI(pool, IdentitySettings{Enabled: true})
	governance := NewGovernanceAPI(pool, identity, strings.Repeat("ab", 32), "internal-test")
	response := identityRequest(identity, "POST", "/api/v2/auth/register", `{"username":"audio_`+strings.ReplaceAll(uuid.NewString(), "-", "")+`","password":"a-secure-password"}`, nil)
	if response.Code != 201 {
		t.Fatal(response.Body.String())
	}
	cookie := response.Result().Cookies()[0]
	var registered struct {
		User Principal `json:"user"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &registered); err != nil {
		t.Fatal(err)
	}
	profile := `{"name":"ElevenLabs media","provider":"elevenlabs","api_key":"audio-test-key","music_models":[{"model_name":"music_v2_5","capabilities":{"output_formats":["mp3_44100_128"],"min_duration_ms":3000,"max_duration_ms":600000,"instrumental":true}}],"sound_effect_models":[{"model_name":"eleven_text_to_sound_v2","capabilities":{"output_formats":["mp3_22050_32"],"min_duration_seconds":0.5,"max_duration_seconds":30,"prompt_influence":true,"loop":true}}]}`
	response = identityRequest(governance, "POST", "/api/model-settings/profiles", profile, cookie)
	if response.Code != 201 {
		t.Fatal(response.Body.String())
	}
	var saved ModelProfile
	if err := json.Unmarshal(response.Body.Bytes(), &saved); err != nil {
		t.Fatal(err)
	}
	response = identityRequest(governance, "POST", "/api/model-settings/profiles", `{"name":"Text and embeddings","provider":"openai_compatible","base_url":"https://models.example/v1","api_key":"text-test-key","chat_models":["writer"],"embedding_models":["embedding"],"max_tokens":4096}`, cookie)
	if response.Code != 201 {
		t.Fatal(response.Body.String())
	}
	var textProfile ModelProfile
	if err := json.Unmarshal(response.Body.Bytes(), &textProfile); err != nil {
		t.Fatal(err)
	}
	targets := map[string]RouteTarget{
		"creative":                {ProfileID: textProfile.ID, ModelName: "writer"},
		"analysis":                {ProfileID: textProfile.ID, ModelName: "writer"},
		"embedding":               {ProfileID: textProfile.ID, ModelName: "embedding"},
		"music_generation":        {ProfileID: saved.ID, ModelName: "music_v2_5"},
		"sound_effect_generation": {ProfileID: saved.ID, ModelName: "eleven_text_to_sound_v2"},
	}
	raw, _ := json.Marshal(targets)
	response = identityRequest(governance, "PUT", "/api/model-settings/routes", string(raw), cookie)
	if response.Code != 200 {
		t.Fatal(response.Body.String())
	}
	var routeResult struct {
		RouteVersionID string `json:"route_version_id"`
	}
	if err := json.Unmarshal(response.Body.Bytes(), &routeResult); err != nil {
		t.Fatal(err)
	}
	frozen, err := governance.ResolveFrozenRoutes(context.Background(), registered.User.WorkspaceID, routeResult.RouteVersionID)
	if err != nil {
		t.Fatal(err)
	}
	for purpose, capability := range map[string]string{"music_generation": "music_capabilities", "sound_effect_generation": "sound_effect_capabilities"} {
		route := frozen.Routes[purpose]
		if route.Provider != "elevenlabs" || route.BaseURL != "https://api.elevenlabs.io/v1" || route.APIKey != "audio-test-key" {
			t.Fatalf("%s route was not frozen correctly: %#v", purpose, route)
		}
		if capability == "music_capabilities" && (route.MusicCapabilities == nil || route.MusicCapabilities.MaxDurationMS != 600000) {
			t.Fatalf("music capabilities missing: %#v", route.MusicCapabilities)
		}
		if capability == "sound_effect_capabilities" && (route.SoundEffectCapabilities == nil || route.SoundEffectCapabilities.MaxDurationSeconds != 30) {
			t.Fatalf("sound effect capabilities missing: %#v", route.SoundEffectCapabilities)
		}
	}
}
