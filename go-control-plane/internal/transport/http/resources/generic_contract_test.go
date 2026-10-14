package resources

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http/httptest"
	"os"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/go-control-plane/internal/contracts"
	executionpostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/execution/postgres"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/identity"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/config"
	"github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/api"
)

func TestGenericContentTypesShareProjectDocumentAndTaskContract(t *testing.T) {
	url := os.Getenv("MQ_TEST_CONTROL_DATABASE_URL")
	if url == "" {
		t.Skip("database integration URL unavailable")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	scope := uuid.NewString()
	if _, err = pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Generic contract')", scope); err != nil {
		t.Fatal(err)
	}
	writer := &executionSnapshotWriterStub{}
	repo := executionpostgres.NewMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"}, executionpostgres.RepositorySettings{SnapshotStore: writer})
	resourceAPI := NewResourceAPI(pool, repo, nil)
	server := api.NewServerWithDependencies(api.ServerDependencies{Resources: resourceAPI})
	call := func(method, path string, body any) *httptest.ResponseRecorder {
		request := httptest.NewRequest(method, path, bytes.NewReader(resourceJSON(body)))
		request.Header.Set("X-Workspace-ID", scope)
		request = request.WithContext(identity.WithPrincipal(request.Context(), identity.Principal{WorkspaceID: scope, Role: "owner"}))
		recorder := httptest.NewRecorder()
		server.ServeHTTP(recorder, request)
		return recorder
	}
	for _, kind := range []string{"novel", "screenplay", "copy"} {
		t.Run(kind, func(t *testing.T) {
			response := call("POST", "/api/v2/projects", map[string]any{"title": kind, "content_type": kind, "input": map[string]any{"objective": "Write content"}, "brief": map[string]any{"audience": "Readers"}})
			if response.Code != 201 {
				t.Fatalf("create %d %s", response.Code, response.Body.String())
			}
			var project ProjectResource
			if err := json.Unmarshal(response.Body.Bytes(), &project); err != nil {
				t.Fatal(err)
			}
			if project.ContentType != kind {
				t.Fatalf("content type=%s", project.ContentType)
			}
			response = call("POST", "/api/v2/documents/"+project.DocumentID+"/versions", map[string]any{"expected_version": 0, "document": map[string]any{"schema_version": "1.0", "blocks": []map[string]any{{"type": "text", "text": "Draft", "position": 0}}}})
			if response.Code != 201 {
				t.Fatalf("version %d %s", response.Code, response.Body.String())
			}
			response = call("POST", "/api/v2/documents/"+project.DocumentID+"/versions", map[string]any{"expected_version": 0, "document": map[string]any{"schema_version": "1.0", "blocks": []any{}}})
			if response.Code != 409 {
				t.Fatalf("stale version %d %s", response.Code, response.Body.String())
			}
			workflow := "content.generate"
			if kind == "novel" {
				workflow = "novel.write"
			}
			response = call("POST", "/api/v2/jobs", map[string]any{"project_id": project.ID, "workflow": workflow, "workflow_version": 1, "arguments": map[string]any{}})
			if response.Code != 202 {
				t.Fatalf("task %d %s", response.Code, response.Body.String())
			}
			var snapshot contracts.ExecutionSnapshot
			if err := json.Unmarshal(writer.body, &snapshot); err != nil {
				t.Fatal(err)
			}
			if snapshot.ProjectID != project.ID {
				t.Fatal("execution snapshot project mismatch")
			}
			if !bytes.Contains(writer.body, []byte(`"audience":"Readers"`)) {
				t.Fatal("brief absent from execution snapshot")
			}
			if bytes.Contains(writer.body, []byte(`"novel_input"`)) || bytes.Contains(writer.body, []byte(`"novel_id"`)) {
				t.Fatal("format-specific identity leaked into Control execution request")
			}
			if !bytes.Contains(writer.body, []byte(`"brief_version":1`)) {
				t.Fatal("brief version missing from execution snapshot")
			}
			var task map[string]any
			if err := json.Unmarshal(response.Body.Bytes(), &task); err != nil {
				t.Fatal(err)
			}
			job := task["job_id"].(string)
			if err := resourceAPI.within(ctx, scope, func(tx pgx.Tx) error {
				_, err := tx.Exec(ctx, "UPDATE jobs SET status='waiting_review',last_event_sequence=2 WHERE id=$1", job)
				return err
			}); err != nil {
				t.Fatal(err)
			}
			response = call("POST", "/api/v2/jobs/"+job+"/resume", map[string]any{"action": "approve", "feedback": "approved"})
			if response.Code != 202 {
				t.Fatalf("resume %d %s", response.Code, response.Body.String())
			}
			first := append([]byte(nil), response.Body.Bytes()...)
			response = call("POST", "/api/v2/jobs/"+job+"/resume", map[string]any{"action": "approve", "feedback": "approved"})
			if response.Code != 202 || !bytes.Equal(first, response.Body.Bytes()) {
				t.Fatalf("resume replay %d %s", response.Code, response.Body.String())
			}
			response = call("POST", "/api/v2/jobs/"+job+"/resume", map[string]any{"action": "revise", "feedback": "different"})
			if response.Code != 409 {
				t.Fatalf("resume payload conflict %d %s", response.Code, response.Body.String())
			}
			if err := resourceAPI.within(ctx, scope, func(tx pgx.Tx) error {
				_, err := tx.Exec(ctx, "UPDATE jobs SET status='waiting_review',last_event_sequence=5 WHERE id=$1", job)
				return err
			}); err != nil {
				t.Fatal(err)
			}
			response = call("POST", "/api/v2/jobs/"+job+"/resume", map[string]any{"action": "approve", "feedback": "approved"})
			if response.Code != 202 || bytes.Equal(first, response.Body.Bytes()) {
				t.Fatalf("next review episode %d %s", response.Code, response.Body.String())
			}
		})
	}
	for _, path := range []string{"/api/v2/novels", "/api/novels", "/internal/v1/jobs", "/internal/v1/artifacts/sign", "/internal/v1/generations"} {
		if response := call("POST", path, map[string]any{}); response.Code != 404 {
			t.Fatalf("retired route %s got %d", path, response.Code)
		}
	}
}
