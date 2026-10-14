package postgres

import (
	"context"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/contracts"
	projectmodule "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/config"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/objectstorage"
)

const videoProfileJSON = `{"name":"video profile","provider":"volcengine","api_key":"video-test-key","chat_models":["chat"],"embedding_models":["embed"],"video_models":[{"model_name":"seedance-a","capabilities":{"text_to_video":true,"image_to_video":true,"durations":[5,10],"ratios":["16:9","9:16"],"resolutions":["720p"],"generate_audio":true,"cancellation":false}},{"model_name":"seedance-b","capabilities":{"text_to_video":true,"image_to_video":false,"durations":[8],"ratios":["1:1"],"resolutions":["1080p"],"generate_audio":false,"cancellation":true}}]}`

func TestVideoProfileRoundTripAndCapabilityValidation(t *testing.T) {
	var input modelProfileWrite
	if err := json.Unmarshal([]byte(videoProfileJSON), &input); err != nil {
		t.Fatal(err)
	}
	if err := validateModelProfile(input); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(input)
	var public map[string]any
	_ = json.Unmarshal(raw, &public)
	models, ok := public["video_models"].([]any)
	if !ok || len(models) != 2 {
		t.Fatalf("video models were discarded: %s", raw)
	}
	for _, bad := range []string{
		strings.Replace(videoProfileJSON, `"durations":[5,10]`, `"durations":[0,10]`, 1),
		strings.Replace(videoProfileJSON, `"durations":[5,10]`, `"durations":[5,121]`, 1),
		strings.Replace(videoProfileJSON, `"durations":[5,10]`, `"durations":[5,5]`, 1),
		strings.Replace(videoProfileJSON, `"ratios":["16:9","9:16"]`, `"ratios":[""]`, 1),
		strings.Replace(videoProfileJSON, `"model_name":"seedance-b"`, `"model_name":"seedance-a"`, 1),
		strings.Replace(videoProfileJSON, `"text_to_video":true,"image_to_video":true`, `"text_to_video":false,"image_to_video":false`, 1),
	} {
		var candidate modelProfileWrite
		if err := json.Unmarshal([]byte(bad), &candidate); err != nil {
			t.Fatal(err)
		}
		if err := validateModelProfile(candidate); err == nil {
			t.Fatalf("invalid capability accepted: %s", bad)
		}
	}
	for _, provider := range []string{"openai", "qwen", "volcengine", "openai_compatible"} {
		input.Provider, input.BaseURL = provider, "https://example.test/v1"
		if err := validateModelProfile(input); err != nil {
			t.Fatalf("governance registry rejected %s: %v", provider, err)
		}
	}
}

func TestVideoGenerationAcceptsOnlyControlledNodesAndParameters(t *testing.T) {
	base := GenerationRequest{ProjectID: uuid.NewString(), DocumentType: "video", Title: "Video", Objective: "A river at dawn"}
	for name, args := range map[string]map[string]any{
		"defaults": nil,
		"full":     {"video": map[string]any{"duration": float64(5), "ratio": "16:9", "resolution": "720p", "generate_audio": true, "image_url": "https://example.test/image.png", "seed": float64(-1), "watermark": false, "camera_fixed": true}},
	} {
		request := base
		request.Arguments = args
		request.Nodes = []string{"plan", "generate", "review"}
		if !validGeneration(request) {
			t.Fatalf("valid video %s rejected", name)
		}
	}
	for name, args := range map[string]map[string]any{
		"unknown outer":     {"tools": []string{"run"}},
		"unknown parameter": {"video": map[string]any{"model": "unfrozen"}},
		"null object":       {"video": nil},
		"duration string":   {"video": map[string]any{"duration": "5"}},
		"duration fraction": {"video": map[string]any{"duration": 5.5}},
		"duration huge":     {"video": map[string]any{"duration": 1e100}},
		"duration zero":     {"video": map[string]any{"duration": 0}},
		"duration negative": {"video": map[string]any{"duration": -1}},
		"audio string":      {"video": map[string]any{"generate_audio": "true"}},
		"ratio empty":       {"video": map[string]any{"ratio": ""}},
		"image credentials": {"video": map[string]any{"image_url": "https://user:secret@example.test/a"}},
		"image scheme":      {"video": map[string]any{"image_url": "file:///tmp/image"}},
		"seed huge":         {"video": map[string]any{"seed": 1e100}},
	} {
		t.Run(name, func(t *testing.T) {
			request := base
			request.Arguments = args
			if validGeneration(request) {
				t.Fatal("invalid video arguments accepted")
			}
		})
	}
	for _, nodes := range [][]string{{"write", "review"}, {"generate"}, {"plan", "generate", "review", "custom"}} {
		request := base
		request.Nodes = nodes
		if validGeneration(request) {
			t.Fatal("uncontrolled video nodes accepted")
		}
	}
	nodes, err := workflowPromptNodes("media.video@1")
	if err != nil || !reflect.DeepEqual(nodes, []string{"media.plan", "media.review", "memory.refresh"}) {
		t.Fatalf("media prompt contract: %v %v", nodes, err)
	}
}

func TestVideoGovernancePostgresFreezesSelectedModelAndLegacyRoutes(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	a := NewIdentityAPI(pool, IdentitySettings{Enabled: true})
	g := NewGovernanceAPI(pool, a, strings.Repeat("ab", 32), "internal-test")
	w := identityRequest(a, "POST", "/api/v2/auth/register", `{"username":"video_`+strings.ReplaceAll(uuid.NewString(), "-", "")+`","password":"a-secure-password"}`, nil)
	if w.Code != 201 {
		t.Fatal(w.Body.String())
	}
	cookie := w.Result().Cookies()[0]
	var registered struct {
		User Principal `json:"user"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &registered)
	workspace := registered.User.WorkspaceID
	t.Cleanup(func() { _, _ = pool.Exec(ctx, "DELETE FROM workspaces WHERE id=$1", workspace) })
	w = identityRequest(g, "POST", "/api/v2/model-settings/profiles", videoProfileJSON, cookie)
	if w.Code != 201 {
		t.Fatalf("video profile: %d %s", w.Code, w.Body.String())
	}
	var profile ModelProfile
	_ = json.Unmarshal(w.Body.Bytes(), &profile)
	targets := map[string]RouteTarget{"creative": {ProfileID: profile.ID, ModelName: "chat"}, "analysis": {ProfileID: profile.ID, ModelName: "chat"}, "embedding": {ProfileID: profile.ID, ModelName: "embed"}}
	raw, _ := json.Marshal(targets)
	w = identityRequest(g, "PUT", "/api/v2/model-settings/routes", string(raw), cookie)
	if w.Code != 200 {
		t.Fatalf("legacy routes: %d %s", w.Code, w.Body.String())
	}
	if _, err := g.FreezeCapabilitySnapshot(ctx, workspace, map[string]any{"model_routes": map[string]string{"video_generation": "current"}}); err == nil || !strings.Contains(err.Error(), "video_generation") {
		t.Fatalf("missing video route was not rejected: %v", err)
	}
	targets["video_generation"] = RouteTarget{ProfileID: profile.ID, ModelName: "seedance-b"}
	raw, _ = json.Marshal(targets)
	w = identityRequest(g, "PUT", "/api/v2/model-settings/routes", string(raw), cookie)
	if w.Code != 200 {
		t.Fatalf("video route: %d %s", w.Code, w.Body.String())
	}
	var saved struct {
		ID string `json:"route_version_id"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &saved)
	w = identityRequest(g, "PUT", "/api/v2/model-settings/profiles/"+profile.ID, strings.ReplaceAll(videoProfileJSON, `"durations":[8]`, `"durations":[10]`), cookie)
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	frozen, err := g.ResolveFrozenRoutes(ctx, workspace, saved.ID)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ = json.Marshal(frozen.Routes["video_generation"])
	var route map[string]any
	_ = json.Unmarshal(raw, &route)
	capabilities, ok := route["video_capabilities"].(map[string]any)
	if !ok || !reflect.DeepEqual(capabilities["durations"], []any{float64(8)}) || capabilities["generate_audio"] != false {
		t.Fatalf("selected historical model capabilities not frozen: %s", raw)
	}
	g.SetPromptCatalog(NewPromptAPI(pool, a, "internal-test"))
	g.SetCreationPolicies(NewCreationPolicyAPI(pool, a, "internal-test"))
	partial, err := g.FreezeCapabilitySnapshot(ctx, workspace, map[string]any{"workflow": "media.video@1", "model_routes": map[string]any{"creative": "current", "video_generation": "current"}})
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(partial["model_routes"], map[string]string{"creative": saved.ID, "video_generation": saved.ID}) {
		t.Fatalf("required route purposes changed: %#v", partial["model_routes"])
	}
	project, err := createProjectFixture(pool, projectmodule.Project{WorkspaceID: workspace, Name: "Video test", ProjectType: "video"})
	if err != nil {
		t.Fatal(err)
	}
	repo := newTestMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"}).WithSnapshotFreezer(g.FreezeCapabilitySnapshot).WithExecutionSnapshotStore(&executionSnapshotWriterStub{})
	command, err := repo.Generate(ctx, workspace, GenerationRequest{ProjectID: project.ID, DocumentType: "video", Title: "River", Objective: "A river at dawn", IdempotencyKey: uuid.NewString(), Arguments: map[string]any{"video": map[string]any{"duration": 8}}})
	if err != nil {
		t.Fatal(err)
	}
	bundle, err := repo.Execution(ctx, workspace, command.JobID)
	if err != nil {
		t.Fatal(err)
	}
	if bundle.Snapshot["workflow"] != "media.video@1" || !reflect.DeepEqual(bundle.Request.Nodes, []string{"plan", "generate", "review"}) || !reflect.DeepEqual(bundle.Snapshot["skills"], []any{"controlled-video@1"}) {
		t.Fatalf("incorrect video execution bundle: %#v", bundle)
	}
	routes := bundle.Snapshot["model_routes"].(map[string]any)
	if len(routes) != 4 || routes["video_generation"] != saved.ID {
		t.Fatalf("video route not frozen in execution: %#v", routes)
	}
	prompts := bundle.Snapshot["prompt_bindings"].(map[string]any)
	if len(prompts) != 3 || prompts["media.plan"] == nil || prompts["media.review"] == nil || prompts["memory.refresh"] == nil {
		t.Fatalf("video prompt binding incomplete: %#v", prompts)
	}
	version := uuid.NewString()
	manifest := map[string]any{"object_key": "video/river.mp4", "sha256": "media-checksum", "media_type": "video/mp4"}
	body, _ := json.Marshal(map[string]any{"document_id": bundle.Request.DocumentID, "project_id": project.ID, "document_type": "video", "version": 1, "schema_version": "1.0", "content_hash": "video-ir-hash", "blocks": []map[string]any{{"type": "media", "text": "A river at dawn", "position": 0, "metadata": map[string]any{"artifact": manifest}}}})
	repo.WithContentArtifacts(generatedArtifact{body: body})
	key, _ := objectstorage.ObjectKey(workspace, project.ID, "documents", bundle.Request.DocumentID, "content.json", version)
	commit, _ := json.Marshal(map[string]any{"document_id": bundle.Request.DocumentID, "document_version_id": version, "ir_hash": "video-ir-hash", "workflow": "media.video@1", "artifact": map[string]any{"object_key": key, "sha256": "checksum"}})
	for index, event := range []contracts.Event{
		{Type: "job.completed", Payload: json.RawMessage(`{}`)},
		{Type: "content.version.committed", Payload: commit},
	} {
		event.SchemaVersion, event.ID, event.WorkspaceID, event.ProjectID = "1.0", uuid.NewString(), workspace, project.ID
		event.JobID, event.WorkflowRunID, event.Sequence = command.JobID, command.WorkflowRunID, int64(index+1)
		raw, _ := json.Marshal(event)
		if err = repo.Project(ctx, raw); err != nil {
			t.Fatal(err)
		}
		if err = repo.Project(ctx, raw); err != nil {
			t.Fatal("event replay", err)
		}
	}
	stored, err := repo.ContentVersion(ctx, workspace, project.ID, version)
	if err != nil {
		t.Fatal(err)
	}
	if stored.Document.DocumentType != "video" || !reflect.DeepEqual(stored.Document.Blocks[0].Metadata["artifact"], manifest) {
		t.Fatalf("video IR manifest did not survive projection: %#v", stored)
	}
	// Omitting the new optional field in a legacy profile edit preserves the registry.
	var legacyEdit map[string]any
	_ = json.Unmarshal([]byte(videoProfileJSON), &legacyEdit)
	delete(legacyEdit, "video_models")
	raw, _ = json.Marshal(legacyEdit)
	w = identityRequest(g, "PUT", "/api/v2/model-settings/profiles/"+profile.ID, string(raw), cookie)
	if w.Code != 200 || !strings.Contains(w.Body.String(), "seedance-b") {
		t.Fatalf("legacy edit discarded video registry: %d %s", w.Code, w.Body.String())
	}
	targets["video_generation"] = RouteTarget{ProfileID: profile.ID, ModelName: "chat"}
	raw, _ = json.Marshal(targets)
	w = identityRequest(g, "PUT", "/api/v2/model-settings/routes", string(raw), cookie)
	if w.Code != 422 {
		t.Fatalf("chat model used for video: %d", w.Code)
	}
	targets["video_generation"] = RouteTarget{ProfileID: profile.ID, ModelName: "seedance-a", Fallback: []RouteTarget{{ProfileID: profile.ID, ModelName: "seedance-b"}}}
	raw, _ = json.Marshal(targets)
	w = identityRequest(g, "PUT", "/api/v2/model-settings/routes", string(raw), cookie)
	if w.Code != 200 {
		t.Fatalf("multiple video model fallbacks: %d %s", w.Code, w.Body.String())
	}
	_ = json.Unmarshal(w.Body.Bytes(), &saved)
	frozen, err = g.ResolveFrozenRoutes(ctx, workspace, saved.ID)
	if err != nil {
		t.Fatal(err)
	}
	videoRoute := frozen.Routes["video_generation"]
	if len(videoRoute.Fallback) != 1 || videoRoute.Fallback[0].VideoCapabilities == nil || videoRoute.VideoCapabilities == nil || videoRoute.VideoCapabilities.GenerateAudio == videoRoute.Fallback[0].VideoCapabilities.GenerateAudio {
		t.Fatalf("fallback did not retain its own model capabilities: %#v", videoRoute)
	}
}
