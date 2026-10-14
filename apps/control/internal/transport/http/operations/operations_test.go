package operations

import (
	"crypto/sha256"
	"encoding/hex"
	"os"

	"github.com/jackc/pgx/v5/pgxpool"
	contentpostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/content/postgres"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/evaluation"
	evaluationpostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/evaluation/postgres"
	executionpostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/execution/postgres"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/knowledge"
	knowledgepostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/knowledge/postgres"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/observability"
	observabilitypostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/observability/postgres"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/project"
	projectpostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/project/postgres"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/task"
	"github.com/sakuya/ai-content-platform/apps/control/internal/platform/objectstorage"

	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/sakuya/ai-content-platform/apps/control/internal/contracts"
	"github.com/sakuya/ai-content-platform/apps/control/internal/platform/config"
)

func createProjectFixture(pool *pgxpool.Pool, workspace, name, contentType string) (project.Resource, error) {
	return (projectpostgres.Repository{Pool: pool, Conflict: task.ErrConflict, MarkCandidatesStale: executionpostgres.MarkCandidatesStale, CreateInitialDocument: contentpostgres.CreateDocumentWithin}).Create(
		context.Background(), workspace,
		map[string]any{"title": name, "content_type": contentType, "input": map[string]any{}, "brief": map[string]any{}},
		uuid.NewString(),
	)
}

func TestOperationsRejectsInvalidSearchBeforeAccessingStore(t *testing.T) {
	api := NewOperationsAPI(nil, nil, nil, nil, "")
	request := httptest.NewRequest("POST", "/api/v2/projects/"+uuid.NewString()+"/memory/search", strings.NewReader(`{"query":""}`))
	request = request.WithContext(WithPrincipal(request.Context(), Principal{WorkspaceID: uuid.NewString(), Role: "editor"}))
	response := httptest.NewRecorder()
	api.ServeHTTP(response, request)
	if response.Code != 400 {
		t.Fatalf("status: %d", response.Code)
	}
}

func TestOperationsViewerCannotEnqueueWork(t *testing.T) {
	api := NewOperationsAPI(nil, nil, nil, nil, "")
	request := httptest.NewRequest("POST", "/api/v2/evaluations/benchmarks", strings.NewReader(`{}`))
	request = request.WithContext(WithPrincipal(request.Context(), Principal{WorkspaceID: uuid.NewString(), Role: "viewer"}))
	response := httptest.NewRecorder()
	api.ServeHTTP(response, request)
	if response.Code != 403 {
		t.Fatalf("status: %d", response.Code)
	}
}

func TestOperationErrorsPreserveConflictAndNotFoundResponses(t *testing.T) {
	for _, item := range []struct {
		err    error
		status int
	}{{task.ErrConflict, 409}, {project.ErrNotFound, 404}, {identity.ErrNotFound, 404}} {
		response := httptest.NewRecorder()
		operationError(response, item.err)
		if response.Code != item.status {
			t.Fatalf("status: %d", response.Code)
		}
	}
}

func TestOperationOutboxUsesSnapshotAndReplaysOnlyMatchingRequests(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	workspace := uuid.NewString()
	if _, err := pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Operation test')", workspace); err != nil {
		t.Fatal(err)
	}
	project, err := createProjectFixture(pool, workspace, "Operations", "generic")
	if err != nil {
		t.Fatal(err)
	}
	writer := &executionSnapshotWriterStub{}
	repo := executionpostgres.NewMQRepository(pool, config.MQTopics{Tasks: "tasks"}, executionpostgres.RepositorySettings{SnapshotStore: writer})
	key := uuid.NewString()
	command, err := repo.SubmitOperation(ctx, workspace, project.ID, "memory.search", key, map[string]any{"query": "continuity", "limit": 5})
	if err != nil {
		t.Fatal(err)
	}
	if command.ExecutionSnapshot == nil {
		t.Fatal("operation does not reference an immutable snapshot")
	}
	var stored map[string]any
	if err = json.Unmarshal(writer.body, &stored); err != nil {
		t.Fatal(err)
	}
	if stored["request"].(map[string]any)["runtime_operation"] != "memory.search" {
		t.Fatal("operation was omitted from execution snapshot")
	}
	replay, err := repo.SubmitOperation(ctx, workspace, project.ID, "memory.search", key, map[string]any{"query": "continuity", "limit": 5})
	if err != nil || replay.JobID != command.JobID {
		t.Fatalf("replay: %v", err)
	}
	if _, err = repo.SubmitOperation(ctx, workspace, project.ID, "memory.search", key, map[string]any{"query": "changed", "limit": 5}); err == nil {
		t.Fatal("conflicting replay accepted")
	}
	var count int
	err = withinTest(pool, ctx, workspace, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT count(*) FROM command_outbox WHERE command_id=$1", command.ID).Scan(&count)
	})
	if err != nil || count != 1 {
		t.Fatalf("outbox: %d %v", count, err)
	}
}

func TestOperationCompletionProjectsMemoryAndPreventsCancelledOverwrite(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	workspace := uuid.NewString()
	if _, err := pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Operation projection')", workspace); err != nil {
		t.Fatal(err)
	}
	project, err := createProjectFixture(pool, workspace, "Projection", "generic")
	if err != nil {
		t.Fatal(err)
	}
	repo := executionpostgres.NewMQRepository(pool, config.MQTopics{Tasks: "tasks"}, executionpostgres.RepositorySettings{SnapshotStore: &executionSnapshotWriterStub{}})
	api := NewOperationsAPI(knowledge.NewService(knowledgepostgres.NewRepository(pool)), evaluation.NewService(evaluationpostgres.NewRepository(pool)), observability.NewService(observabilitypostgres.NewRepository(pool)), repo, "")
	command, err := repo.SubmitOperation(ctx, workspace, project.ID, "memory.refresh", uuid.NewString(), map[string]any{})
	if err != nil {
		t.Fatal(err)
	}
	event := contracts.Event{SchemaVersion: "1.0", ID: uuid.NewString(), Type: "job.completed", WorkspaceID: workspace, ProjectID: project.ID, JobID: command.JobID, WorkflowRunID: command.JobID, OperationID: command.JobID, TraceID: command.ID, Sequence: 1, Payload: json.RawMessage(`{"operation":"memory.refresh","operation_result":{"memory":{"items":[{"text":"A fact"}]},"usage":{"calls":3},"traces":{"items":[]}}}`)}
	body, _ := json.Marshal(event)
	if err = repo.Project(ctx, body); err != nil {
		t.Fatal(err)
	}
	if err = repo.Project(ctx, body); err != nil {
		t.Fatal(err)
	}
	var count int
	err = withinTest(pool, ctx, workspace, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT count(*) FROM project_records WHERE project_id=$1 AND kind LIKE 'runtime_%'", project.ID).Scan(&count)
	})
	if err != nil || count != 4 {
		t.Fatalf("projections: %d %v", count, err)
	}
	request := httptest.NewRequest("GET", "/api/v2/projects/"+project.ID+"/memory", nil)
	request = request.WithContext(WithPrincipal(request.Context(), Principal{WorkspaceID: workspace, Role: "viewer"}))
	response := httptest.NewRecorder()
	api.ServeHTTP(response, request)
	if response.Code != 200 || !strings.Contains(response.Body.String(), "A fact") {
		t.Fatalf("projection read: %d %s", response.Code, response.Body.String())
	}
	command, err = repo.SubmitOperation(ctx, workspace, project.ID, "memory.refresh", uuid.NewString(), map[string]any{})
	if err != nil {
		t.Fatal(err)
	}
	if err = withinTest(pool, ctx, workspace, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, "UPDATE jobs SET status='cancelled' WHERE id=$1", command.JobID)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	event.ID, event.JobID, event.WorkflowRunID = uuid.NewString(), command.JobID, command.JobID
	body, _ = json.Marshal(event)
	if err = repo.Project(ctx, body); err != nil {
		t.Fatal(err)
	}
	err = withinTest(pool, ctx, workspace, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT count(*) FROM project_records WHERE source_job_id=$1", command.JobID).Scan(&count)
	})
	if err != nil || count != 0 {
		t.Fatalf("cancelled task projected: %d %v", count, err)
	}
}

func TestKnowledgeAndBenchmarkCompletionsPopulateControlQueries(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	workspace := uuid.NewString()
	if _, err := pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Knowledge projection')", workspace); err != nil {
		t.Fatal(err)
	}
	project, err := createProjectFixture(pool, workspace, "Knowledge", "generic")
	if err != nil {
		t.Fatal(err)
	}
	writer := &executionSnapshotWriterStub{}
	repo := executionpostgres.NewMQRepository(pool, config.MQTopics{Tasks: "tasks"}, executionpostgres.RepositorySettings{
		SnapshotStore: writer, BenchmarkWriter: evaluationpostgres.RecordBenchmark,
		BenchmarkReportResolver: evaluationpostgres.ResolveBenchmarkReport,
	})
	api := NewOperationsAPI(knowledge.NewService(knowledgepostgres.NewRepository(pool)), evaluation.NewService(evaluationpostgres.NewRepository(pool)), observability.NewService(observabilitypostgres.NewRepository(pool)), repo, "")
	pack := uuid.NewString()
	var benchmarkID string
	for _, item := range []struct {
		operation string
		args      map[string]any
		result    any
	}{
		{"knowledge.publish", map[string]any{"name": "Facts"}, map[string]any{"id": pack, "name": "Facts", "source_project_id": project.ID}},
		{"knowledge.bind", map[string]any{"pack_id": pack}, map[string]any{"shared": true}},
		{"benchmark", map[string]any{}, map[string]any{"passed": true}},
	} {
		command, err := repo.SubmitOperation(ctx, workspace, project.ID, item.operation, uuid.NewString(), item.args)
		if err != nil {
			t.Fatal(err)
		}
		if item.operation == "benchmark" {
			benchmarkID = command.JobID
		}
		payload, _ := json.Marshal(map[string]any{"operation": item.operation, "operation_result": item.result})
		event := contracts.Event{SchemaVersion: "1.0", ID: uuid.NewString(), Type: "job.completed", WorkspaceID: workspace, ProjectID: project.ID, JobID: command.JobID, WorkflowRunID: command.JobID, OperationID: command.JobID, TraceID: command.ID, Sequence: 1, Payload: payload}
		body, _ := json.Marshal(event)
		if err = repo.Project(ctx, body); err != nil {
			t.Fatal(err)
		}
	}
	writer.body = nil
	_, err = repo.SubmitOperation(ctx, workspace, project.ID, "benchmark", uuid.NewString(), map[string]any{"baseline_run_id": benchmarkID})
	if err != nil {
		t.Fatalf("resolve evaluation baseline: %v", err)
	}
	if !strings.Contains(string(writer.body), `"baseline"`) || !strings.Contains(string(writer.body), `"passed":true`) {
		t.Fatalf("benchmark baseline was not frozen in content snapshot: %s", writer.body)
	}
	for _, path := range []string{"/api/v2/knowledge-packs", "/api/v2/projects/" + project.ID + "/knowledge-packs", "/api/v2/evaluations/benchmarks"} {
		request := httptest.NewRequest("GET", path, nil)
		request = request.WithContext(WithPrincipal(request.Context(), Principal{WorkspaceID: workspace, Role: "viewer"}))
		response := httptest.NewRecorder()
		api.ServeHTTP(response, request)
		if response.Code != 200 || response.Body.String() == "{\"items\":[]}\n" {
			t.Fatalf("projection query: %d %s", response.Code, response.Body.String())
		}
		request = request.WithContext(WithPrincipal(request.Context(), Principal{WorkspaceID: uuid.NewString(), Role: "viewer"}))
		response = httptest.NewRecorder()
		api.ServeHTTP(response, request)
		if strings.Contains(response.Body.String(), pack) || strings.Contains(response.Body.String(), "passed") {
			t.Fatal("cross-workspace projection visible")
		}
	}
}

func TestOperationsReadsFreshUsageAndTraceEvents(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	workspace := uuid.NewString()
	if _, err := pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Live usage')", workspace); err != nil {
		t.Fatal(err)
	}
	project, err := createProjectFixture(pool, workspace, "Usage", "copy")
	if err != nil {
		t.Fatal(err)
	}
	err = withinTest(pool, ctx, workspace, func(tx pgx.Tx) error {
		for _, kind := range []string{"usage", "trace"} {
			data := `{"input_tokens":10,"output_tokens":5,"provider":"example","model_name":"writer"}`
			if _, err := tx.Exec(ctx, "INSERT INTO project_records(workspace_id,project_id,kind,record_key,payload,content_hash) VALUES($1,$2,$3,$4,$5,$6)", workspace, project.ID, kind, uuid.NewString(), data, resourceHash([]byte(data))); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	api := NewOperationsAPI(knowledge.NewService(knowledgepostgres.NewRepository(pool)), evaluation.NewService(evaluationpostgres.NewRepository(pool)), observability.NewService(observabilitypostgres.NewRepository(pool)), nil, "")
	for _, kind := range []string{"usage", "traces"} {
		request := httptest.NewRequest("GET", "/api/v2/projects/"+project.ID+"/"+kind, nil)
		request = request.WithContext(WithPrincipal(request.Context(), Principal{WorkspaceID: workspace, Role: "viewer"}))
		response := httptest.NewRecorder()
		api.ServeHTTP(response, request)
		if response.Code != 200 || !strings.Contains(response.Body.String(), "10") {
			t.Fatalf("fresh %s projection: %d %s", kind, response.Code, response.Body.String())
		}
	}
}

type Principal = identity.Principal

var WithPrincipal = identity.WithPrincipal

func resourceHash(data []byte) string { sum := sha256.Sum256(data); return hex.EncodeToString(sum[:]) }
func identityTestPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	url := os.Getenv("MQ_TEST_CONTROL_DATABASE_URL")
	if url == "" {
		t.Skip("MQ_TEST_CONTROL_DATABASE_URL is not configured")
	}
	pool, err := pgxpool.New(context.Background(), url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return pool
}
func withinTest(pool *pgxpool.Pool, ctx context.Context, workspace string, fn func(pgx.Tx) error) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if _, err = tx.Exec(ctx, "SELECT set_config('app.workspace_id',$1,true)", workspace); err != nil {
		return err
	}
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

type executionSnapshotWriterStub struct{ body []byte }

func (w *executionSnapshotWriterStub) Put(_ context.Context, key string, body []byte, kind string) (objectstorage.ArtifactReference, error) {
	w.body = append([]byte(nil), body...)
	return objectstorage.ArtifactReference{Bucket: "agent-artifacts", ObjectKey: key, SHA256: resourceHash(body), SizeBytes: int64(len(body)), ContentType: kind}, nil
}
