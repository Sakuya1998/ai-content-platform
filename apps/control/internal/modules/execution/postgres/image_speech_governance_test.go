package postgres

import (
	"context"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/google/uuid"
	projectmodule "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/config"
)

const imageSpeechProfileJSON = `{"name":"media profile","provider":"openai","api_key":"media-test-key","chat_models":["chat"],"embedding_models":["embed"],"image_models":[{"model_name":"image-a","capabilities":{"generation":true,"editing":true,"sizes":["1024x1024"],"formats":["png","webp"],"max_images":4}},{"model_name":"image-b","capabilities":{"generation":true,"editing":false,"sizes":["512x512"],"formats":["jpeg"],"max_images":1}}],"speech_models":[{"model_name":"speech-a","capabilities":{"voices":["alloy"],"formats":["mp3","wav"],"languages":["en","zh"],"min_speed":0.25,"max_speed":4,"max_input_chars":16000,"instructions":true}},{"model_name":"speech-b","capabilities":{"voices":["nova"],"formats":["opus"],"languages":["en"],"min_speed":1,"max_speed":2,"max_input_chars":1000,"instructions":false}}]}`

func TestImageSpeechProfileCapabilitiesAndJSONTypes(t *testing.T) {
	var input modelProfileWrite
	if err := json.Unmarshal([]byte(imageSpeechProfileJSON), &input); err != nil {
		t.Fatal(err)
	}
	if err := validateModelProfile(input); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(input)
	var profile map[string]any
	_ = json.Unmarshal(raw, &profile)
	for _, field := range []string{"image_models", "speech_models"} {
		if values, ok := profile[field].([]any); !ok || len(values) != 2 {
			t.Fatalf("discarded %s: %s", field, raw)
		}
	}
	for _, replacement := range [][2]string{
		{`"max_images":4`, `"max_images":0`}, {`"max_images":4`, `"max_images":5`},
		{`"generation":true,"editing":true`, `"generation":false,"editing":false`},
		{`"generation":true`, `"generation":"true"`}, {`"generation":true`, `"generation":null`},
		{`"sizes":["1024x1024"]`, `"sizes":[]`}, {`"formats":["png","webp"]`, `"formats":["gif"]`},
		{`"model_name":"image-b"`, `"model_name":"image-a"`},
		{`"voices":["alloy"]`, `"voices":["alloy","alloy"]`},
		{`"formats":["mp3","wav"]`, `"formats":["video"]`},
		{`"min_speed":0.25`, `"min_speed":0.1`}, {`"max_speed":4`, `"max_speed":0.1`},
		{`"max_input_chars":16000`, `"max_input_chars":16001`},
		{`"instructions":true`, `"instructions":null`}, {`"min_speed":0.25`, `"min_speed":"0.25"`},
	} {
		bad := strings.Replace(imageSpeechProfileJSON, replacement[0], replacement[1], 1)
		var candidate modelProfileWrite
		if err := json.Unmarshal([]byte(bad), &candidate); err == nil && validateModelProfile(candidate) == nil {
			t.Fatalf("invalid capabilities accepted: %s", bad)
		}
	}
	if base, ok := providerURL("volcengine_tts"); !ok || base != "https://openspeech.bytedance.com" {
		t.Fatalf("speech provider missing: %q %v", base, ok)
	}
}

func TestImageSpeechGenerationRequestBounds(t *testing.T) {
	for _, kind := range []string{"image", "audio"} {
		base := GenerationRequest{ProjectID: uuid.NewString(), DocumentType: kind, Title: "Media", Objective: "A river at dawn"}
		if !validGeneration(base) {
			t.Fatalf("%s defaults rejected", kind)
		}
		base.Nodes = []string{"plan", "generate", "review"}
		if !validGeneration(base) {
			t.Fatalf("%s controlled nodes rejected", kind)
		}
		base.Nodes = []string{"plan", "write", "review"}
		if validGeneration(base) {
			t.Fatalf("%s text nodes accepted", kind)
		}
	}
	for _, args := range []map[string]any{
		{"image": map[string]any{"operation": "generate", "size": "1024x1024", "format": "png", "n": json.Number("4")}},
		{"image": map[string]any{"operation": "edit", "n": 1}},
	} {
		r := GenerationRequest{ProjectID: uuid.NewString(), DocumentType: "image", Title: "Image", Objective: "River", SourceVersionID: uuid.NewString(), Arguments: args}
		if !validGeneration(r) {
			t.Fatalf("valid image rejected: %#v", args)
		}
	}
	for _, parameters := range []map[string]any{{"operation": "edit"}, {"n": "1"}, {"n": true}, {"n": 1.5}, {"n": 0}, {"n": 5}, {"format": "gif"}, {"operation": "other"}, {"size": ""}, {"image_url": "https://example.test/a"}, {"format": nil}} {
		r := GenerationRequest{ProjectID: uuid.NewString(), DocumentType: "image", Title: "Image", Objective: "River", Arguments: map[string]any{"image": parameters}}
		if validGeneration(r) {
			t.Fatalf("invalid image accepted: %#v", parameters)
		}
	}
	for _, speed := range []any{0.25, 1.5, 4, json.Number("2.5")} {
		r := GenerationRequest{ProjectID: uuid.NewString(), DocumentType: "audio", Title: "Audio", Objective: "River", Arguments: map[string]any{"audio": map[string]any{"voice": "alloy", "format": "mp3", "speed": speed, "language": "zh", "instructions": "Calm", "text": "江水", "character_id": "narrator", "scene_id": "dawn"}}}
		if !validGeneration(r) {
			t.Fatalf("valid speech rejected: %#v", r.Arguments)
		}
	}
	for _, parameters := range []map[string]any{{"speed": "1"}, {"speed": true}, {"speed": 0.24}, {"speed": 4.1}, {"format": "mp4"}, {"text": strings.Repeat("文", 16001)}, {"voice": ""}, {"instructions": true}, {"text": nil}, {"model": "unfrozen"}, {"character_id": 3}} {
		r := GenerationRequest{ProjectID: uuid.NewString(), DocumentType: "audio", Title: "Audio", Objective: "River", Arguments: map[string]any{"audio": parameters}}
		if validGeneration(r) {
			t.Fatalf("invalid speech accepted: %#v", parameters)
		}
	}
	for _, item := range []struct{ workflow, node string }{{"media.image@1", "media.image.plan"}, {"media.speech@1", "media.speech.plan"}} {
		nodes, err := workflowPromptNodes(item.workflow)
		if err != nil || !reflect.DeepEqual(nodes, []string{item.node, "media.review", "memory.refresh"}) {
			t.Fatalf("%s prompt nodes: %v %v", item.workflow, nodes, err)
		}
	}
}

func TestImageSpeechGovernancePostgresFrozenCapabilitiesAndExecution(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	identity := NewIdentityAPI(pool, IdentitySettings{Enabled: true})
	governance := NewGovernanceAPI(pool, identity, strings.Repeat("ab", 32), "internal-test")
	response := identityRequest(identity, "POST", "/api/v2/auth/register", `{"username":"media_`+strings.ReplaceAll(uuid.NewString(), "-", "")+`","password":"a-secure-password"}`, nil)
	if response.Code != 201 {
		t.Fatal(response.Body.String())
	}
	cookie := response.Result().Cookies()[0]
	var registration struct {
		User Principal `json:"user"`
	}
	_ = json.Unmarshal(response.Body.Bytes(), &registration)
	workspace := registration.User.WorkspaceID
	t.Cleanup(func() { _, _ = pool.Exec(ctx, "DELETE FROM workspaces WHERE id=$1", workspace) })
	response = identityRequest(governance, "POST", "/api/v2/model-settings/profiles", imageSpeechProfileJSON, cookie)
	if response.Code != 201 {
		t.Fatalf("media profile: %d %s", response.Code, response.Body.String())
	}
	if strings.Contains(response.Body.String(), "media-test-key") {
		t.Fatal("profile leaked media API key")
	}
	var profile ModelProfile
	_ = json.Unmarshal(response.Body.Bytes(), &profile)
	targets := map[string]RouteTarget{"creative": {ProfileID: profile.ID, ModelName: "chat"}, "analysis": {ProfileID: profile.ID, ModelName: "chat"}, "embedding": {ProfileID: profile.ID, ModelName: "embed"}}
	saveRoutes := func() string {
		t.Helper()
		body, _ := json.Marshal(targets)
		response = identityRequest(governance, "PUT", "/api/v2/model-settings/routes", string(body), cookie)
		if response.Code != 200 {
			t.Fatalf("save routes: %d %s", response.Code, response.Body.String())
		}
		var result struct {
			ID string `json:"route_version_id"`
		}
		_ = json.Unmarshal(response.Body.Bytes(), &result)
		return result.ID
	}
	saveRoutes()
	for _, purpose := range []string{"image_generation", "speech_synthesis"} {
		_, err := governance.FreezeCapabilitySnapshot(ctx, workspace, map[string]any{"model_routes": map[string]string{purpose: "current"}})
		if err == nil || !strings.Contains(err.Error(), purpose) {
			t.Fatalf("missing %s not rejected: %v", purpose, err)
		}
	}
	targets["image_generation"] = RouteTarget{ProfileID: profile.ID, ModelName: "image-b", Fallback: []RouteTarget{{ProfileID: profile.ID, ModelName: "image-a"}}}
	targets["speech_synthesis"] = RouteTarget{ProfileID: profile.ID, ModelName: "speech-b", Fallback: []RouteTarget{{ProfileID: profile.ID, ModelName: "speech-a"}}}
	routeID := saveRoutes()
	changed := strings.Replace(imageSpeechProfileJSON, `"max_input_chars":1000`, `"max_input_chars":2000`, 1)
	changed = strings.Replace(changed, `"sizes":["512x512"]`, `"sizes":["2048x2048"]`, 1)
	response = identityRequest(governance, "PUT", "/api/v2/model-settings/profiles/"+profile.ID, changed, cookie)
	if response.Code != 200 {
		t.Fatalf("profile update: %d %s", response.Code, response.Body.String())
	}
	frozen, err := governance.ResolveFrozenRoutes(ctx, workspace, routeID)
	if err != nil {
		t.Fatal(err)
	}
	imageRoute, speechRoute := frozen.Routes["image_generation"], frozen.Routes["speech_synthesis"]
	if imageRoute.ImageCapabilities == nil || !reflect.DeepEqual(imageRoute.ImageCapabilities.Sizes, []string{"512x512"}) || len(imageRoute.Fallback) != 1 || imageRoute.Fallback[0].ImageCapabilities == nil || !imageRoute.Fallback[0].ImageCapabilities.Editing {
		t.Fatalf("historical image capabilities/fallback changed: %#v", imageRoute)
	}
	if speechRoute.SpeechCapabilities == nil || speechRoute.SpeechCapabilities.MaxInputChars != 1000 || len(speechRoute.Fallback) != 1 || speechRoute.Fallback[0].SpeechCapabilities == nil || !speechRoute.Fallback[0].SpeechCapabilities.Instructions {
		t.Fatalf("historical speech capabilities/fallback changed: %#v", speechRoute)
	}
	if imageRoute.SpeechCapabilities != nil || speechRoute.ImageCapabilities != nil {
		t.Fatal("unrelated capabilities exposed")
	}
	governance.SetPromptCatalog(NewPromptAPI(pool, identity, "internal-test"))
	governance.SetCreationPolicies(NewCreationPolicyAPI(pool, identity, "internal-test"))
	for _, item := range []struct{ kind, workflow, purpose, skill, node string }{
		{"image", "media.image@1", "image_generation", "controlled-image@1", "media.image.plan"},
		{"audio", "media.speech@1", "speech_synthesis", "controlled-speech@1", "media.speech.plan"},
	} {
		partial, err := governance.FreezeCapabilitySnapshot(ctx, workspace, map[string]any{"workflow": item.workflow, "model_routes": map[string]any{"creative": "current", item.purpose: "current"}})
		if err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(partial["model_routes"], map[string]string{"creative": routeID, item.purpose: routeID}) {
			t.Fatalf("requested route purposes changed: %#v", partial["model_routes"])
		}
		project, err := createProjectFixture(pool, projectmodule.Project{WorkspaceID: workspace, Name: "Media test", ProjectType: "generic"})
		if err != nil {
			t.Fatal(err)
		}
		repo := newTestMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"}).WithSnapshotFreezer(governance.FreezeCapabilitySnapshot).WithExecutionSnapshotStore(&executionSnapshotWriterStub{})
		request := GenerationRequest{ProjectID: project.ID, DocumentType: item.kind, Title: "River", Objective: "River at dawn", IdempotencyKey: uuid.NewString()}
		command, err := repo.Generate(ctx, workspace, request)
		if err != nil {
			t.Fatal(err)
		}
		bundle, err := repo.Execution(ctx, workspace, command.JobID)
		if err != nil {
			t.Fatal(err)
		}
		if bundle.Snapshot["workflow"] != item.workflow || !reflect.DeepEqual(bundle.Request.Nodes, []string{"plan", "generate", "review"}) || !reflect.DeepEqual(bundle.Snapshot["skills"], []any{item.skill}) {
			t.Fatalf("invalid media execution bundle: %#v", bundle)
		}
		routes := bundle.Snapshot["model_routes"].(map[string]any)
		if len(routes) != 4 || routes[item.purpose] != routeID {
			t.Fatalf("media route missing: %#v", routes)
		}
		prompts := bundle.Snapshot["prompt_bindings"].(map[string]any)
		if len(prompts) != 3 || prompts[item.node] == nil || prompts["media.review"] == nil || prompts["memory.refresh"] == nil {
			t.Fatalf("media prompt binding incomplete: %#v", prompts)
		}
		replay, err := repo.Generate(ctx, workspace, request)
		if err != nil || replay.ID != command.ID {
			t.Fatalf("media request replay changed: %v %#v", err, replay)
		}
	}
	// Old clients may omit media registries on edits; only explicit [] clears them.
	var legacy map[string]any
	_ = json.Unmarshal([]byte(imageSpeechProfileJSON), &legacy)
	delete(legacy, "image_models")
	delete(legacy, "speech_models")
	raw, _ := json.Marshal(legacy)
	response = identityRequest(governance, "PUT", "/api/v2/model-settings/profiles/"+profile.ID, string(raw), cookie)
	if response.Code != 200 || !strings.Contains(response.Body.String(), "image-b") || !strings.Contains(response.Body.String(), "speech-b") {
		t.Fatalf("legacy edit discarded media models: %d %s", response.Code, response.Body.String())
	}
	for _, purpose := range []string{"image_generation", "speech_synthesis"} {
		prior := targets[purpose]
		targets[purpose] = RouteTarget{ProfileID: profile.ID, ModelName: "chat"}
		raw, _ = json.Marshal(targets)
		response = identityRequest(governance, "PUT", "/api/v2/model-settings/routes", string(raw), cookie)
		if response.Code != 422 {
			t.Fatalf("chat model accepted for %s: %d", purpose, response.Code)
		}
		targets[purpose] = prior
	}
	credentials := `{"app_id":"tts-test-app","access_token":"tts-test-token"}`
	legacy["provider"] = "volcengine_tts"
	legacy["api_key"] = credentials
	raw, _ = json.Marshal(legacy)
	response = identityRequest(governance, "POST", "/api/v2/model-settings/profiles", string(raw), cookie)
	if response.Code != 201 || strings.Contains(response.Body.String(), "tts-test-token") || strings.Contains(response.Body.String(), "tts-test-app") {
		t.Fatalf("TTS credentials exposed/rejected: %d %s", response.Code, response.Body.String())
	}
	var tts ModelProfile
	_ = json.Unmarshal(response.Body.Bytes(), &tts)
	legacy["speech_models"] = []any{map[string]any{"model_name": "tts-resource-id", "capabilities": map[string]any{"voices": []string{"narrator"}, "formats": []string{"mp3"}, "languages": []string{"zh"}, "min_speed": 0.25, "max_speed": 4, "max_input_chars": 16000, "instructions": false}}}
	raw, _ = json.Marshal(legacy)
	response = identityRequest(governance, "PUT", "/api/v2/model-settings/profiles/"+tts.ID, string(raw), cookie)
	if response.Code != 200 {
		t.Fatalf("TTS model update: %d %s", response.Code, response.Body.String())
	}
	targets["speech_synthesis"] = RouteTarget{ProfileID: tts.ID, ModelName: "tts-resource-id"}
	frozen, err = governance.ResolveFrozenRoutes(ctx, workspace, saveRoutes())
	if err != nil {
		t.Fatal(err)
	}
	if route := frozen.Routes["speech_synthesis"]; route.BaseURL != "https://openspeech.bytedance.com" || route.APIKey != credentials {
		t.Fatal("TTS route did not resolve opaque credentials and dedicated endpoint")
	}
}
