package postgres

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/sakuya/novel-agent/apps/control/internal/contracts"
	contentpostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/content/postgres"
	projectmodule "github.com/sakuya/novel-agent/apps/control/internal/modules/project"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/config"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/objectstorage"
	resources "github.com/sakuya/novel-agent/apps/control/internal/transport/http/resources"
)

func TestImmutableContentHTTPAndExecutionPreserveFloatHash(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	workspace := uuid.NewString()
	if _, err := pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Numeric IR test')", workspace); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(ctx, "DELETE FROM workspaces WHERE id=$1", workspace) })
	project, err := createProjectFixture(pool, projectmodule.Project{WorkspaceID: workspace, Name: "Numeric IR", ProjectType: "generic"})
	if err != nil {
		t.Fatal(err)
	}
	repo := newTestMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"}).WithExecutionSnapshotStore(&executionSnapshotWriterStub{})
	server := resources.NewHandler(resources.Dependencies{Content: contentpostgres.Repository{Pool: pool}})
	for _, kind := range []string{"timeline", "audio"} {
		documentID, versionID := uuid.NewString(), uuid.NewString()
		metadata := `{"parameters":{"speed":1.0}}`
		if kind == "timeline" {
			metadata = `{"timeline":{"assets":[{"asset_id":"river","block_position":0,"document_version_id":"00000000-0000-0000-0000-000000000001"}],"audio_tracks":[{"asset_id":"river","duration_ms":1000,"gain":1.0,"start_ms":0,"track_id":"voice"}],"duration_ms":1000,"fps":25,"height":720,"schema_version":"timeline@1","shots":[{"asset_id":"river","duration_ms":1000,"shot_id":"shot","start_ms":0}],"subtitles":[],"width":1280}}`
		}
		// Hand-written canonical Python Content IR bytes keep the expected hash
		// independent of the Go serializer exercised by the HTTP response.
		canonical := fmt.Sprintf(`{"blocks":[{"metadata":{"parameters":{"speed":1.0}},"position":0,"text":"River","type":"media"}],"document_id":"%s","document_type":"%s","metadata":%s,"project_id":"%s","schema_version":"1.0","version":1}`, documentID, kind, metadata, project.ID)
		sum := sha256.Sum256([]byte(canonical))
		hash := hex.EncodeToString(sum[:])
		body := canonical[:len(canonical)-1] + `,"content_hash":"` + hash + `"}`
		tx, err := workspaceTx(ctx, pool, workspace)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec(ctx, "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES($1,$2,$3,$4,$5)", documentID, workspace, project.ID, kind, documentID); err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec(ctx, "INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,content_hash,payload,status) VALUES($1,$2,$3,1,'1.0',$4,$5,'approved')", versionID, workspace, documentID, hash, body); err != nil {
			t.Fatal(err)
		}
		if err := tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
		request := httptest.NewRequest("GET", "/api/v2/content/versions/"+versionID+"?project_id="+project.ID, nil)
		request = request.WithContext(WithPrincipal(request.Context(), Principal{WorkspaceID: workspace, Role: "owner"}))
		request.Header.Set("Authorization", "Bearer numeric-ir-test")
		request.Header.Set("X-Workspace-ID", workspace)
		response := httptest.NewRecorder()
		server.ServeHTTP(response, request)
		if response.Code != 200 {
			t.Fatal(response.Body.String())
		}
		if !bytes.Contains(response.Body.Bytes(), []byte(`"speed":1.0`)) || kind == "timeline" && !bytes.Contains(response.Body.Bytes(), []byte(`"gain":1.0`)) {
			t.Fatalf("immutable numeric JSON lexemes changed: %s", response.Body.String())
		}
		decoder := json.NewDecoder(response.Body)
		decoder.UseNumber()
		var document map[string]any
		if err := decoder.Decode(&document); err != nil {
			t.Fatal(err)
		}
		if document["content_hash"] != hash {
			t.Fatal("stored IR hash changed")
		}
		delete(document, "content_hash")
		encoded, err := canonicalPromptJSON(document)
		if err != nil {
			t.Fatal(err)
		}
		actual := sha256.Sum256(encoded)
		if hex.EncodeToString(actual[:]) != hash {
			t.Fatal("wire IR hash differs from immutable original")
		}
		command, err := repo.Generate(ctx, workspace, GenerationRequest{ProjectID: project.ID, DocumentType: "storyboard", SourceVersionID: versionID, InputVersionIDs: []string{versionID}, Title: uuid.NewString(), Objective: "Plan shots"})
		if err != nil {
			t.Fatal(err)
		}
		bundle, err := repo.Execution(ctx, workspace, command.JobID)
		if err != nil {
			t.Fatal(err)
		}
		encoded, _ = json.Marshal(bundle.Source)
		if !bytes.Contains(encoded, []byte(`"speed":1.0`)) || kind == "timeline" && !bytes.Contains(encoded, []byte(`"gain":1.0`)) {
			t.Fatal("execution source lost immutable numeric lexemes")
		}
		encoded, _ = json.Marshal(bundle.Inputs)
		if !bytes.Contains(encoded, []byte(`"speed":1.0`)) || kind == "timeline" && !bytes.Contains(encoded, []byte(`"gain":1.0`)) {
			t.Fatal("execution inputs lost immutable numeric lexemes")
		}
	}
}

func timelineRequest(t *testing.T, kind, operation string, args map[string]any, inputs []string) GenerationRequest {
	t.Helper()
	body, _ := json.Marshal(map[string]any{"project_id": uuid.NewString(), "document_type": kind, "operation": operation, "title": "Timeline", "objective": "Make a scene", "source_version_id": uuid.NewString(), "input_version_ids": inputs, "arguments": args})
	var request GenerationRequest
	if err := json.Unmarshal(body, &request); err != nil {
		t.Fatal(err)
	}
	return request
}

func TestTimelinePostgresInputsSnapshotsAndDerivations(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	workspace := uuid.NewString()
	if _, err := pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Timeline test')", workspace); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(ctx, "DELETE FROM workspaces WHERE id=$1", workspace) })
	project, err := createProjectFixture(pool, projectmodule.Project{WorkspaceID: workspace, Name: "Timeline project", ProjectType: "generic"})
	if err != nil {
		t.Fatal(err)
	}
	foreign, err := createProjectFixture(pool, projectmodule.Project{WorkspaceID: workspace, Name: "Other project", ProjectType: "generic"})
	if err != nil {
		t.Fatal(err)
	}
	insert := func(project, kind string, metadata ...map[string]any) string {
		t.Helper()
		tx, err := workspaceTx(ctx, pool, workspace)
		if err != nil {
			t.Fatal(err)
		}
		defer tx.Rollback(ctx)
		document, version := uuid.NewString(), uuid.NewString()
		var documentMetadata map[string]any
		if len(metadata) > 0 {
			documentMetadata = metadata[0]
		}
		payload, _ := json.Marshal(GeneratedDocument{DocumentID: document, ProjectID: project, DocumentType: kind, Version: 1, SchemaVersion: "1.0", ContentHash: version, Blocks: []GeneratedBlock{{Type: "media", Text: "River", Position: 0, Metadata: map[string]any{}}}, Metadata: documentMetadata})
		if _, err := tx.Exec(ctx, "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES($1,$2,$3,$4,$5)", document, workspace, project, kind, document); err != nil {
			t.Fatal(err)
		}
		if _, err := tx.Exec(ctx, "INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,content_hash,payload,status) VALUES($1,$2,$3,1,'1.0',$5,$4,'approved')", version, workspace, document, payload, version); err != nil {
			t.Fatal(err)
		}
		if err := tx.Commit(ctx); err != nil {
			t.Fatal(err)
		}
		return version
	}
	image, audio := insert(project.ID, "image"), insert(project.ID, "audio")
	manifest := explicitTimeline(t)
	manifest["assets"].([]any)[0].(map[string]any)["document_version_id"] = image
	source, other := insert(project.ID, "timeline", map[string]any{"timeline": manifest}), insert(foreign.ID, "image")
	repo := newTestMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"}).WithExecutionSnapshotStore(&executionSnapshotWriterStub{})
	for _, item := range []struct {
		kind, operation, source, workflow, skill string
		nodes                                    []string
	}{
		{"storyboard", "", "", "media.storyboard@1", "controlled-storyboard@1", []string{"plan", "review"}},
		{"timeline", "", "", "media.timeline@1", "controlled-timeline@1", []string{"plan", "review"}},
		{"video", "compose", source, "media.compose@1", "controlled-compose@1", []string{"validate", "render", "publish"}},
	} {
		request := GenerationRequest{ProjectID: project.ID, DocumentType: item.kind, Operation: item.operation, SourceVersionID: item.source, InputVersionIDs: []string{audio, image}, Title: uuid.NewString(), Objective: "Make a river scene", IdempotencyKey: uuid.NewString()}
		command, err := repo.Generate(ctx, workspace, request)
		if err != nil {
			t.Fatal(err)
		}
		bundle, err := repo.Execution(ctx, workspace, command.JobID)
		if err != nil {
			t.Fatal(err)
		}
		if bundle.Snapshot["workflow"] != item.workflow || !reflect.DeepEqual(bundle.Request.Nodes, item.nodes) || !reflect.DeepEqual(bundle.Snapshot["skills"], []any{item.skill}) {
			t.Fatalf("workflow snapshot wrong: %#v", bundle)
		}
		routes := bundle.Snapshot["model_routes"].(map[string]any)
		if len(routes) != 2 || routes["analysis"] == nil || routes["embedding"] == nil {
			t.Fatalf("unexpected routes: %#v", routes)
		}
		if len(bundle.Inputs) != 2 || bundle.Inputs[0].VersionID != audio || bundle.Inputs[1].VersionID != image {
			t.Fatalf("input order lost: %#v", bundle.Inputs)
		}
		replay, err := repo.Generate(ctx, workspace, request)
		if err != nil || replay.ID != command.ID {
			t.Fatalf("replay failed: %v", err)
		}
	}
	for _, request := range []GenerationRequest{
		{ProjectID: project.ID, DocumentType: "timeline", Title: uuid.NewString(), Objective: "A scene", InputVersionIDs: []string{other}},
		{ProjectID: project.ID, DocumentType: "video", Operation: "compose", Title: uuid.NewString(), Objective: "A scene", SourceVersionID: image},
		{ProjectID: project.ID, DocumentType: "video", Operation: "compose", Title: uuid.NewString(), Objective: "A scene", SourceVersionID: source, InputVersionIDs: []string{audio}},
	} {
		if _, err := repo.Generate(ctx, workspace, request); err == nil {
			t.Fatal("foreign input, non-timeline source, or unfrozen timeline asset accepted")
		}
	}
	request := GenerationRequest{ProjectID: project.ID, DocumentType: "video", Operation: "compose", SourceVersionID: source, InputVersionIDs: []string{source, audio, image}, Title: uuid.NewString(), Objective: "A scene"}
	command, err := repo.Generate(ctx, workspace, request)
	if err != nil {
		t.Fatal(err)
	}
	_ = json.Unmarshal(command.Payload, &request)
	version := uuid.NewString()
	body, _ := json.Marshal(GeneratedDocument{DocumentID: request.DocumentID, ProjectID: project.ID, DocumentType: "video", Version: 1, SchemaVersion: "1.0", ContentHash: "composed-hash", Blocks: []GeneratedBlock{{Type: "media", Text: "Composed video", Position: 0}}})
	repo.WithContentArtifacts(generatedArtifact{body: body})
	key, _ := objectstorage.ObjectKey(workspace, project.ID, "documents", request.DocumentID, "content.json", version)
	commit, _ := json.Marshal(map[string]any{"document_id": request.DocumentID, "document_version_id": version, "ir_hash": "composed-hash", "workflow": "media.compose@1", "source_version_id": source, "source_ir_hash": source, "input_version_ids": request.InputVersionIDs, "input_ir_hashes": map[string]string{source: source, audio: audio, image: image}, "artifact": map[string]any{"object_key": key, "sha256": "checksum"}})
	for i, item := range []struct {
		event   string
		payload json.RawMessage
	}{{"job.completed", json.RawMessage(`{}`)}, {"content.version.committed", commit}} {
		event := contracts.Event{SchemaVersion: "1.0", ID: uuid.NewString(), Type: item.event, WorkspaceID: workspace, ProjectID: project.ID, JobID: command.JobID, WorkflowRunID: command.WorkflowRunID, Sequence: int64(i + 1), Payload: item.payload}
		data, _ := json.Marshal(event)
		if err := repo.Project(ctx, data); err != nil {
			t.Fatal(err)
		}
		if err := repo.Project(ctx, data); err != nil {
			t.Fatal(err)
		}
	}
	var count int
	tx, err := workspaceTx(ctx, pool, workspace)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if err := tx.QueryRow(ctx, "SELECT count(*) FROM derivations WHERE target_version_id=$1", version).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 3 {
		t.Fatalf("immutable source/input derivations lost or duplicated: %d", count)
	}
}

func TestTimelinePostgresGovernanceFreezesPromptPolicyAndOnlyRequiredRoutes(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	identity := NewIdentityAPI(pool, IdentitySettings{Enabled: true})
	governance := NewGovernanceAPI(pool, identity, strings.Repeat("ab", 32), "internal-test")
	response := identityRequest(identity, "POST", "/api/v2/auth/register", `{"username":"timeline_`+strings.ReplaceAll(uuid.NewString(), "-", "")+`","password":"a-secure-password"}`, nil)
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
	response = identityRequest(governance, "POST", "/api/v2/model-settings/profiles", `{"name":"timeline-profile","provider":"openai","api_key":"timeline-test-key","chat_models":["chat"],"embedding_models":["embed"]}`, cookie)
	if response.Code != 201 {
		t.Fatal(response.Body.String())
	}
	var profile ModelProfile
	_ = json.Unmarshal(response.Body.Bytes(), &profile)
	// A client configuring only the historical three routes can compose existing assets.
	data, _ := json.Marshal(map[string]RouteTarget{"creative": {ProfileID: profile.ID, ModelName: "chat"}, "analysis": {ProfileID: profile.ID, ModelName: "chat"}, "embedding": {ProfileID: profile.ID, ModelName: "embed"}})
	response = identityRequest(governance, "PUT", "/api/v2/model-settings/routes", string(data), cookie)
	if response.Code != 200 {
		t.Fatal(response.Body.String())
	}
	governance.SetPromptCatalog(NewPromptAPI(pool, identity, "internal-test"))
	governance.SetCreationPolicies(NewCreationPolicyAPI(pool, identity, "internal-test"))
	for _, workflow := range []string{"media.storyboard@1", "media.timeline@1", "media.compose@1"} {
		frozen, err := governance.FreezeCapabilitySnapshot(ctx, workspace, map[string]any{"workflow": workflow, "model_routes": map[string]string{"analysis": "current", "embedding": "current"}})
		if err != nil {
			t.Fatal(err)
		}
		routes := frozen["model_routes"].(map[string]string)
		if len(routes) != 2 || routes["analysis"] == "" || routes["embedding"] == "" {
			t.Fatalf("unneeded routes required: %#v", routes)
		}
		prompts := frozen["prompt_bindings"].(map[string]string)
		expected, _ := workflowPromptNodes(workflow)
		if len(prompts) != len(expected) || frozen["creation_policy"] == nil {
			t.Fatalf("incomplete governance snapshot: %#v", frozen)
		}
		for _, node := range expected {
			if prompts[node] == "" {
				t.Fatalf("missing immutable prompt: %s", node)
			}
		}
	}
}

func TestStoryboardTimelineComposeGenerationContract(t *testing.T) {
	for _, kind := range []string{"storyboard", "timeline"} {
		r := timelineRequest(t, kind, "", nil, nil)
		r.Nodes = []string{"plan", "review"}
		if !validGeneration(r) {
			t.Fatalf("%s plan/review rejected", kind)
		}
		r.Nodes = []string{"plan", "generate", "review"}
		if validGeneration(r) {
			t.Fatalf("%s provider nodes accepted", kind)
		}
	}
	r := timelineRequest(t, "video", "compose", map[string]any{"compose": map[string]any{"preset": "draft"}}, nil)
	r.Nodes = []string{"validate", "render", "publish"}
	if !validGeneration(r) {
		t.Fatal("compose request rejected")
	}
	for _, mutate := range []func(*GenerationRequest){
		func(r *GenerationRequest) { r.SourceVersionID = "" },
		func(r *GenerationRequest) { r.DocumentType = "audio" },
		func(r *GenerationRequest) { r.Operation = "shell" },
		func(r *GenerationRequest) { r.Arguments = map[string]any{"compose": map[string]any{"preset": "ultra"}} },
		func(r *GenerationRequest) {
			r.Arguments = map[string]any{"compose": map[string]any{"command": "ffmpeg"}}
		},
		func(r *GenerationRequest) { r.Nodes = []string{"plan", "generate", "review"} },
	} {
		bad := r
		mutate(&bad)
		if validGeneration(bad) {
			t.Fatalf("invalid compose accepted: %#v", bad)
		}
	}
	for _, parameters := range []map[string]any{{"max_shots": 129}, {"max_shots": true}, {"target_duration_ms": 600001}, {"target_duration_ms": 1.5}, {"url": "https://example.test"}} {
		if validGeneration(timelineRequest(t, "storyboard", "", map[string]any{"storyboard": parameters}, nil)) {
			t.Fatalf("invalid storyboard parameters: %#v", parameters)
		}
	}
	if !validGeneration(timelineRequest(t, "storyboard", "", map[string]any{"storyboard": map[string]any{"max_shots": 128, "target_duration_ms": 600000}}, nil)) {
		t.Fatal("bounded storyboard args rejected")
	}
}

func TestGenerationInputIDsMustBeBoundedUniqueUUIDs(t *testing.T) {
	good := []string{uuid.NewString(), uuid.NewString()}
	if !validGeneration(timelineRequest(t, "timeline", "", nil, good)) {
		t.Fatal("valid inputs rejected")
	}
	for _, inputs := range [][]string{{"not-a-version"}, {good[0], good[0]}, {good[0], strings.ToUpper(good[0])}, make([]string, 257)} {
		if validGeneration(timelineRequest(t, "timeline", "", nil, inputs)) {
			t.Fatalf("invalid input IDs accepted: %v", inputs)
		}
	}
}

func TestLegacyGenerationCannotClaimUnusedMultiInputProvenance(t *testing.T) {
	for _, kind := range []string{"copy", "image", "audio", "video", "novel", "screenplay"} {
		request := timelineRequest(t, kind, "", nil, []string{uuid.NewString()})
		if validGeneration(request) {
			t.Fatalf("%s accepted unsupported multi-input provenance", kind)
		}
		request.InputVersionIDs = nil
		if !validGeneration(request) {
			t.Fatalf("%s legacy request rejected", kind)
		}
	}
}

func TestCommittedInputsCannotChangeFrozenIDsOrHashes(t *testing.T) {
	a, b := uuid.NewString(), uuid.NewString()
	request := GenerationRequest{InputVersionIDs: []string{a, b}}
	versions := []ContentVersion{{VersionID: a, Document: GeneratedDocument{ContentHash: "hash-a"}}, {VersionID: b, Document: GeneratedDocument{ContentHash: "hash-b"}}}
	if !validCommittedInputs(request, []string{a, b}, map[string]string{a: "hash-a", b: "hash-b"}, versions) {
		t.Fatal("frozen inputs rejected")
	}
	if !validCommittedInputs(request, nil, nil, versions) {
		t.Fatal("legacy commit must derive from frozen request inputs")
	}
	for _, item := range []struct {
		ids    []string
		hashes map[string]string
	}{
		{[]string{b, a}, map[string]string{a: "hash-a", b: "hash-b"}},
		{[]string{a}, map[string]string{a: "hash-a"}},
		{[]string{a, b}, map[string]string{a: "changed", b: "hash-b"}},
		{[]string{a, b}, map[string]string{a: "hash-a"}},
		{[]string{a, b}, map[string]string{a: "hash-a", b: "hash-b", uuid.NewString(): "hash"}},
	} {
		if validCommittedInputs(request, item.ids, item.hashes, versions) {
			t.Fatalf("changed committed inputs accepted: %#v", item)
		}
	}
}

func TestTimelineWorkflowPromptContracts(t *testing.T) {
	for workflow, expected := range map[string][]string{
		"media.storyboard@1": {"media.storyboard.plan", "media.review", "memory.refresh"},
		"media.timeline@1":   {"media.timeline.plan", "media.review", "memory.refresh"},
		"media.compose@1":    {"memory.refresh"},
	} {
		nodes, err := workflowPromptNodes(workflow)
		if err != nil || !reflect.DeepEqual(nodes, expected) {
			t.Fatalf("%s nodes: %v %v", workflow, nodes, err)
		}
	}
}

func explicitTimeline(t *testing.T) map[string]any {
	t.Helper()
	var value map[string]any
	_ = json.Unmarshal([]byte(`{"schema_version":"timeline@1","width":1280,"height":720,"fps":25,"duration_ms":4000,"assets":[{"asset_id":"river","document_version_id":"00000000-0000-0000-0000-000000000001","block_position":0}],"shots":[{"shot_id":"s1","asset_id":"river","start_ms":0,"duration_ms":4000}]}`), &value)
	return value
}

func TestExplicitTimelineValidatesReferencesContinuityAndClosedObjects(t *testing.T) {
	if !validGeneration(timelineRequest(t, "timeline", "", map[string]any{"timeline": explicitTimeline(t)}, nil)) {
		t.Fatal("valid explicit timeline rejected")
	}
	for _, mutate := range []func(map[string]any){
		func(v map[string]any) { v["width"] = 1279 },
		func(v map[string]any) { v["height"] = 1920; v["width"] = 1920 },
		func(v map[string]any) { v["fps"] = true },
		func(v map[string]any) { v["duration_ms"] = 600001 },
		func(v map[string]any) { v["command"] = "ffmpeg" },
		func(v map[string]any) {
			v["assets"].([]any)[0].(map[string]any)["document_version_id"] = "https://example.test"
		},
		func(v map[string]any) { v["assets"].([]any)[0].(map[string]any)["url"] = "https://example.test" },
		func(v map[string]any) { v["shots"].([]any)[0].(map[string]any)["asset_id"] = "missing" },
		func(v map[string]any) { v["shots"].([]any)[0].(map[string]any)["start_ms"] = 1 },
		func(v map[string]any) { v["shots"].([]any)[0].(map[string]any)["duration_ms"] = 3999 },
		func(v map[string]any) { v["shots"].([]any)[0].(map[string]any)["fit"] = "stretch" },
		func(v map[string]any) {
			v["subtitles"] = []any{map[string]any{"cue_id": "c1", "start_ms": 0, "end_ms": 4001, "text": "river"}}
		},
		func(v map[string]any) {
			v["audio_tracks"] = []any{map[string]any{"track_id": "a1", "asset_id": "river", "start_ms": 0, "duration_ms": 4000, "gain": 3}}
		},
		func(v map[string]any) {
			v["audio_tracks"] = []any{map[string]any{"track_id": "a1", "asset_id": "river", "start_ms": 0, "duration_ms": 4000, "gain": nil}}
		},
		func(v map[string]any) {
			v["subtitles"] = []any{map[string]any{"cue_id": "c1", "start_ms": 0, "end_ms": 4000, "text": "river\nline"}}
		},
		func(v map[string]any) { v["assets"].([]any)[0].(map[string]any)["asset_id"] = "unsafe/asset" },
		func(v map[string]any) {
			v["shots"] = []any{map[string]any{"shot_id": "s2", "asset_id": "river", "start_ms": 2000, "duration_ms": 2000}, map[string]any{"shot_id": "s1", "asset_id": "river", "start_ms": 0, "duration_ms": 2000}}
		},
	} {
		v := explicitTimeline(t)
		mutate(v)
		if validGeneration(timelineRequest(t, "timeline", "", map[string]any{"timeline": v}, nil)) {
			t.Fatalf("invalid timeline accepted: %#v", v)
		}
	}
}
