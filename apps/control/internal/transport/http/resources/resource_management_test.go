package resources

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/ai-content-platform/apps/control/internal/contracts"

	contentpostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/content/postgres"
	executionpostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/execution/postgres"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/sakuya/ai-content-platform/apps/control/internal/platform/config"
)

func TestResourceImportRejectsTraversalAndTampering(t *testing.T) {
	for _, input := range []string{`{"schema_version":"1.0","project":{"title":"Book","inspiration":"Idea"},"files":[{"path":"../secret","sha256":"x"}]}`, `{"schema_version":"1.0","project":{"title":"Book","inspiration":"Idea"},"sha256":"wrong"}`} {
		if _, err := decodeResourceBackup([]byte(input)); err == nil {
			t.Fatalf("unsafe backup accepted: %s", input)
		}
	}
}

type resourceFixtureReader struct{ body []byte }

func (r resourceFixtureReader) Get(context.Context, string, string) ([]byte, error) {
	return r.body, nil
}

func TestResourceProjectionVersionsSSEAndBackup(t *testing.T) {
	url := os.Getenv("MQ_TEST_CONTROL_DATABASE_URL")
	if url == "" {
		t.Skip("MQ_TEST_CONTROL_DATABASE_URL is not configured")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	scope := uuid.NewString()
	if _, err = pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Projection test')", scope); err != nil {
		t.Fatal(err)
	}
	reader := &mutableArtifactReader{}
	repo := executionpostgres.NewMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"}, executionpostgres.RepositorySettings{SnapshotStore: &executionSnapshotWriterStub{}, Projector: executionpostgres.NewEventProjector(reader, 50<<20, contentpostgres.PersistContentRecord), PrepareContentDocument: contentpostgres.PrepareGeneratedDocument, CommitContentVersion: contentpostgres.CommitGeneratedVersion, ReadContentVersion: contentpostgres.ReadVersionWithin, VersionIDBySourceJob: contentpostgres.VersionIDBySourceJob})
	api := NewResourceAPI(pool, repo, nil)
	request := func(method, path string, body []byte) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, bytes.NewReader(body))
		req.Header.Set("X-Workspace-ID", scope)
		req = req.WithContext(identity.WithPrincipal(req.Context(), identity.Principal{WorkspaceID: scope, Role: "owner"}))
		rec := httptest.NewRecorder()
		api.ServeHTTP(rec, req)
		return rec
	}
	rec := request("POST", "/api/v2/projects", []byte(`{"title":"Story","content_type":"novel","input":{"inspiration":"A journey"}}`))
	if rec.Code != 201 {
		t.Fatal(rec.Body.String())
	}
	var n ProjectResource
	_ = json.Unmarshal(rec.Body.Bytes(), &n)
	command, err := api.submitOperation(ctx, scope, n, "run", map[string]any{}, "run")
	if err != nil {
		t.Fatal(err)
	}
	envelope := ContentEnvelope{State: map[string]any{"project_id": n.ID, "current_phase": "human_review", "canon": map[string]any{"schema_version": "1.0"}}, ContentVersions: []map[string]any{{"block_index": 1, "version_number": 1, "content": "before"}, {"block_index": 1, "version_number": 2, "content": "after"}}, Candidates: []map[string]any{{"candidate_id": "candidate-1", "block_index": 1, "content": "Alternative"}}, Evaluations: []map[string]any{{"id": "eval1", "block_index": 1, "overall_score": 70}, {"id": "eval2", "block_index": 1, "overall_score": 82}}}
	body := resourceJSON(envelope)
	reader.reader = resourceFixtureReader{body: body}
	stateEvent := contracts.Event{SchemaVersion: "1.0", ID: uuid.NewString(), WorkspaceID: scope, ProjectID: n.ID, JobID: command.JobID, WorkflowRunID: command.WorkflowRunID, Sequence: 2, Type: "project.state.changed", Payload: resourceJSON(map[string]any{"project_id": n.ID, "state_hash": resourceHash(resourceJSON(envelope.State)), "artifact": map[string]any{"bucket": "agent-artifacts", "object_key": "workspaces/" + scope + "/projects/" + n.ID + "/assets/novel-state/versions/v1/state.json", "sha256": resourceHash(body)}})}
	if err = repo.Project(ctx, resourceJSON(stateEvent)); err != nil {
		t.Fatal(err)
	}
	rec = request("GET", "/api/v2/projects/"+n.ID+"/projection", nil)
	if strings.Contains(rec.Body.String(), "human_review") {
		t.Fatal("out of order state was projected")
	}
	first := stateEvent
	first.ID = uuid.NewString()
	first.Sequence = 1
	first.Type = "job.running"
	first.Payload = json.RawMessage(`{}`)
	if err = repo.Project(ctx, resourceJSON(first)); err != nil {
		t.Fatal(err)
	}
	rec = request("GET", "/api/v2/projects/"+n.ID+"/blocks/1/versions/diff?from_version=1&to_version=2", nil)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "-before") || !strings.Contains(rec.Body.String(), "+after") {
		t.Fatalf("diff %d %s", rec.Code, rec.Body.String())
	}
	rec = request("PUT", "/api/v2/projects/"+n.ID+"/blocks/1/evaluations/eval1/baseline", []byte(`{}`))
	if rec.Code != 200 {
		t.Fatalf("baseline %d %s", rec.Code, rec.Body.String())
	}
	rec = request("GET", "/api/v2/projects/"+n.ID+"/blocks/1/evaluations/compare?target_id=eval2", nil)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"score_delta":12`) {
		t.Fatalf("compare %d %s", rec.Code, rec.Body.String())
	}
	last := stateEvent
	last.ID = uuid.NewString()
	last.Sequence = 3
	last.Type = "job.waiting_review"
	last.Payload = json.RawMessage(`{}`)
	if err = repo.Project(ctx, resourceJSON(last)); err != nil {
		t.Fatal(err)
	}
	rec = request("GET", "/api/v2/jobs/"+command.JobID+"/events?after_sequence=1", nil)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "id: 2") || strings.Contains(rec.Body.String(), "id: 1") {
		t.Fatalf("SSE %d %s", rec.Code, rec.Body.String())
	}
	rec = request("PUT", "/api/v2/projects/"+n.ID+"/brief", []byte(`{"brief":{"notes":"Updated"},"expected_version":1}`))
	if rec.Code != 200 {
		t.Fatalf("brief %d %s", rec.Code, rec.Body.String())
	}
	rec = request("GET", "/api/v2/projects/"+n.ID+"/export?format=backup", nil)
	if rec.Code != 200 {
		t.Fatalf("export %d %s", rec.Code, rec.Body.String())
	}
	backup, err := decodeResourceBackupZIP(rec.Body.Bytes())
	if err != nil || len(backup.Envelope.ContentVersions) != 2 {
		t.Fatalf("backup %v %#v", err, backup)
	}
	if backup.Envelope.Candidates[0]["stale"] != true {
		t.Fatal("backup discarded stale candidate metadata")
	}
	baselineFound := false
	for _, evaluation := range backup.Envelope.Evaluations {
		if evaluation["baseline"] == true {
			baselineFound = true
		}
	}
	if !baselineFound {
		t.Fatal("backup discarded evaluation baseline")
	}
	rec = request("POST", "/api/v2/projects/import", resourceJSON(backup))
	if rec.Code != 201 {
		t.Fatalf("import %d %s", rec.Code, rec.Body.String())
	}
	var imported map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &imported)
	importedResource := imported["project"].(map[string]any)
	if importedResource["id"] == n.ID {
		t.Fatal("import reused source identity")
	}
	importedID := importedResource["id"].(string)
	var importedDoc ProjectResource
	var importedEnvelope ContentEnvelope
	err = api.within(ctx, scope, func(tx pgx.Tx) error {
		var err error
		importedDoc, err = loadResource(ctx, tx, scope, importedID, false)
		if err != nil {
			return err
		}
		importedEnvelope, err = readResourceEnvelope(ctx, tx, scope, importedDoc)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, rows := range importedEnvelope.Groups() {
		for _, row := range rows {
			if row["project_id"] != importedID {
				t.Fatalf("imported child wrong project %#v", row)
			}
		}
	}
	importCommand, err := api.submitOperation(ctx, scope, importedDoc, "run", map[string]any{}, "import-run")
	if err != nil {
		t.Fatal(err)
	}
	importBody := resourceJSON(importedEnvelope)
	reader.reader = resourceFixtureReader{body: importBody}
	importEvent := stateEvent
	importEvent.ID = uuid.NewString()
	importEvent.ProjectID = importedID
	importEvent.JobID = importCommand.JobID
	importEvent.WorkflowRunID = importCommand.WorkflowRunID
	importEvent.Sequence = 1
	importEvent.Payload = resourceJSON(map[string]any{"project_id": importedID, "artifact": map[string]any{"bucket": "agent-artifacts", "object_key": "workspaces/" + scope + "/projects/" + importedID + "/assets/novel-state/state.json", "sha256": resourceHash(importBody)}})
	if err = repo.Project(ctx, resourceJSON(importEvent)); err != nil {
		t.Fatalf("restored import projection stalled: %v", err)
	}
	rec = request("POST", "/api/v2/projects/import", resourceJSON(backup))
	if rec.Code != 201 {
		t.Fatalf("import replay %d %s", rec.Code, rec.Body.String())
	}
	transferID := uuid.NewString()
	transferBackup, err := api.buildBackup(ctx, scope, n.ID)
	if err != nil {
		t.Fatal(err)
	}
	api.configureTransferEncryptionKey(strings.Repeat("01", 32))
	pending, err := api.encodePendingTransfer(transferBackup, map[string]string{"author": "Writer"}, "password")
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(pending, []byte("password")) {
		t.Fatal("transfer password stored plaintext")
	}
	err = api.within(ctx, scope, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, "INSERT INTO project_transfers(id,workspace_id,project_id,status,format,result,updated_at) VALUES($1,$2,$3,'running','backup',$4,now()-interval '2 minutes')", transferID, scope, n.ID, pending)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	if err = api.RecoverTransfers(ctx); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		rec = request("GET", "/api/v2/transfers/"+transferID, nil)
		var transfer map[string]any
		_ = json.Unmarshal(rec.Body.Bytes(), &transfer)
		if transfer["status"] == "completed" {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("transfer recovery %d %s", rec.Code, rec.Body.String())
		}
		time.Sleep(10 * time.Millisecond)
	}
	rec = request("GET", "/api/v2/transfers/"+transferID+"/download", nil)
	if rec.Code != 200 || !bytes.HasPrefix(rec.Body.Bytes(), []byte(resourceEncryptedMagic)) {
		t.Fatalf("encrypted recovered download %d", rec.Code)
	}
}

func TestResourcePublicationAndEncryptedBackup(t *testing.T) {
	backup := ResourceBackup{SchemaVersion: "1.0", ProjectResource: map[string]any{"title": "Book <one>", "content_type": "copy", "input": map[string]any{}, "brief": map[string]any{}}, Envelope: ContentEnvelope{State: map[string]any{"blocks": []any{map[string]any{"block_index": 1, "title": "Opening", "content": "Text & more"}}}}}
	backup.SHA256 = backupHash(backup)
	for _, format := range []string{"docx", "epub"} {
		body, err := renderResourcePublication(backup, format, nil)
		if err != nil || !bytes.HasPrefix(body, []byte("PK")) {
			t.Fatalf("invalid %s: %v", format, err)
		}
		decoded, err := decodeResourcePublication(body, format, "book."+format)
		if err != nil || len(resourceChapters(decoded)) != 1 || !strings.Contains(resourceText(resourceChapters(decoded)[0]["content"]), "Text & more") {
			t.Fatalf("publication import %s %#v %v", format, decoded, err)
		}
	}
	zipped, err := encodeResourceBackupZIP(backup)
	if err != nil {
		t.Fatal(err)
	}
	encrypted, err := encryptResourceBackup(zipped, "password")
	if err != nil {
		t.Fatal(err)
	}
	if _, err = decryptResourceBackup(encrypted, "wrong"); err == nil {
		t.Fatal("wrong password accepted")
	}
	plain, err := decryptResourceBackup(encrypted, "password")
	if err != nil || !bytes.Equal(plain, zipped) {
		t.Fatalf("round trip %v", err)
	}
	decoded, err := decodeResourceBackupZIP(plain)
	if err != nil || decoded.ProjectResource["title"] != backup.ProjectResource["title"] {
		t.Fatalf("ZIP round trip %#v %v", decoded, err)
	}
}

func TestResourceBriefValidationAndEvaluationComparison(t *testing.T) {
	brief, err := normalizeCommonBrief(map[string]any{"audience": "Editors", "campaign": "Autumn"})
	if err != nil || brief["campaign"] != "Autumn" {
		t.Fatalf("brief %#v %v", brief, err)
	}
	if _, err = normalizeCommonBrief(nil); err == nil {
		t.Fatal("nil brief accepted")
	}
	comparison := compareResourceEvaluations(map[string]any{"overall_score": 80.0, "deterministic_scores": map[string]any{"coherence": 90.0}}, map[string]any{"overall_score": 70.0, "deterministic_scores": map[string]any{"coherence": 75.0}}, 3)
	if comparison["status"] != "regressed" || comparison["overall_delta"] != float64(-10) {
		t.Fatalf("comparison %#v", comparison)
	}
}

func TestResourceScopeAndBodyLimits(t *testing.T) {
	api := NewResourceAPI(nil, nil, nil)
	for _, tc := range []struct {
		path, body, scope string
		status            int
	}{
		{"/api/v2/projects", "{}", "", http.StatusUnauthorized},
		{"/api/v2/projects", string(bytes.Repeat([]byte("x"), int(maxResourceImportBytes+1))), uuid.NewString(), http.StatusRequestEntityTooLarge},
	} {
		req := httptest.NewRequest(http.MethodPost, tc.path, bytes.NewBufferString(tc.body))
		req.Header.Set("X-Workspace-ID", tc.scope)
		req = req.WithContext(identity.WithPrincipal(req.Context(), identity.Principal{WorkspaceID: tc.scope, Role: "owner"}))
		rec := httptest.NewRecorder()
		api.ServeHTTP(rec, req)
		if rec.Code != tc.status {
			t.Fatalf("got %d want %d: %s", rec.Code, tc.status, rec.Body.String())
		}
	}
}

func TestResourceRoleAndUntrustedHeaderDenied(t *testing.T) {
	api := NewResourceAPI(nil, nil, nil)
	scope := uuid.NewString()
	for _, role := range []string{"", "viewer", "unknown"} {
		req := httptest.NewRequest("POST", "/api/v2/projects", bytes.NewBufferString(`{}`))
		req.Header.Set("X-Workspace-ID", scope)
		if role != "" {
			req = req.WithContext(identity.WithPrincipal(req.Context(), identity.Principal{WorkspaceID: scope, Role: role}))
		}
		rec := httptest.NewRecorder()
		api.ServeHTTP(rec, req)
		want := 403
		if role == "" {
			want = 401
		}
		if rec.Code != want {
			t.Fatalf("role %q got %d", role, rec.Code)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, _, err := api.readJobEvents(ctx, scope, uuid.NewString(), 0)
	if err == nil {
		t.Fatal("cancelled stream accessed unavailable database")
	}
}

func TestResourcePostgresLifecycle(t *testing.T) {
	url := os.Getenv("MQ_TEST_CONTROL_DATABASE_URL")
	if url == "" {
		t.Skip("MQ_TEST_CONTROL_DATABASE_URL is not configured")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	workspace := uuid.NewString()
	if _, err = pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'ProjectResource test')", workspace); err != nil {
		t.Fatal(err)
	}
	api := NewResourceAPI(pool, executionpostgres.NewMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"}, executionpostgres.RepositorySettings{SnapshotStore: &executionSnapshotWriterStub{}}), nil)
	call := func(method, path, body, key, scope string) (int, map[string]any) {
		t.Helper()
		req := httptest.NewRequest(method, path, bytes.NewBufferString(body))
		req.Header.Set("X-Workspace-ID", scope)
		req.Header.Set("Idempotency-Key", key)
		req = req.WithContext(identity.WithPrincipal(req.Context(), identity.Principal{WorkspaceID: scope, Role: "owner"}))
		rec := httptest.NewRecorder()
		api.ServeHTTP(rec, req)
		var data map[string]any
		_ = json.Unmarshal(rec.Body.Bytes(), &data)
		return rec.Code, data
	}
	code, novel := call("POST", "/api/v2/projects", `{"title":"Book","content_type":"novel","input":{"inspiration":"Idea","total_chapters":3}}`, "create", workspace)
	if code != 201 {
		t.Fatalf("create %d %#v", code, novel)
	}
	id := novel["id"].(string)
	code, replay := call("POST", "/api/v2/projects", `{"title":"Book","content_type":"novel","input":{"inspiration":"Idea","total_chapters":3}}`, "create", workspace)
	if code != 201 || replay["id"] != id {
		t.Fatalf("create replay %#v", replay)
	}
	code, _ = call("GET", "/api/v2/projects/"+id, "", "", uuid.NewString())
	if code != 404 {
		t.Fatalf("cross workspace %d", code)
	}
	code, _ = call("PUT", "/api/v2/projects/"+id+"/brief", `{"brief":{"notes":"New"},"expected_version":1}`, "", workspace)
	if code != 200 {
		t.Fatalf("brief %d", code)
	}
	code, _ = call("PUT", "/api/v2/projects/"+id+"/brief", `{"brief":{"notes":"Old"},"expected_version":1}`, "", workspace)
	if code != 409 {
		t.Fatalf("stale brief %d", code)
	}
	code, job := call("POST", "/api/v2/projects/"+id+"/jobs/run", `{}`, "run", workspace)
	if code != 202 {
		t.Fatalf("run %d %#v", code, job)
	}
	code, replay = call("POST", "/api/v2/projects/"+id+"/jobs/run", `{}`, "run", workspace)
	if code != 202 || replay["job_id"] != job["job_id"] {
		t.Fatalf("run replay %d %#v", code, replay)
	}
	code, _ = call("POST", "/api/v2/projects/"+id+"/jobs/run", `{}`, "second", workspace)
	if code != 409 {
		t.Fatalf("concurrent job %d", code)
	}
	code, _ = call("POST", "/api/v2/jobs/"+job["job_id"].(string)+"/cancel", `{}`, "", workspace)
	if code != 202 {
		t.Fatalf("cancel %d", code)
	}
}
