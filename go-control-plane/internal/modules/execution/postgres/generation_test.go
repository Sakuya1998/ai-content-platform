package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/go-control-plane/internal/contracts"
	projectmodule "github.com/sakuya/novel-agent/go-control-plane/internal/modules/project"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/config"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/objectstorage"
)

type generatedArtifact struct{ body []byte }

func (a generatedArtifact) Get(context.Context, string, string) ([]byte, error) { return a.body, nil }

func TestGenerationFreezesSnapshotAndProjectsIRExactlyOnce(t *testing.T) {
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
	if _, err = pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Generation test')", workspace); err != nil {
		t.Fatal(err)
	}
	project, err := createProjectFixture(pool, projectmodule.Project{WorkspaceID: workspace, Name: "Generation test", ProjectType: "generic"})
	if err != nil {
		t.Fatal(err)
	}
	repo := newTestMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"}).WithExecutionSnapshotStore(&executionSnapshotWriterStub{})
	requestInput := GenerationRequest{ProjectID: project.ID, DocumentType: "screenplay", Title: "Scene", Objective: "Write a scene", IdempotencyKey: uuid.NewString()}
	if err = repo.within(ctx, workspace, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES($1,$2,$3,'screenplay',$4)", uuid.NewString(), workspace, project.ID, requestInput.Title)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	command, err := repo.Generate(ctx, workspace, requestInput)
	if err != nil {
		t.Fatal(err)
	}
	repo.WithSnapshotFreezer(func(context.Context, string, map[string]any) (map[string]any, error) {
		return nil, errors.New("resource was unpublished after submission")
	})
	repo.WithExecutionResources(func(context.Context, string, map[string]any) (map[string]any, error) {
		return nil, errors.New("provider was disabled after submission")
	})
	replayed, err := repo.Generate(ctx, workspace, requestInput)
	if err != nil || replayed.ID != command.ID {
		t.Fatalf("generation retry created another job: %v", err)
	}
	conflicting := requestInput
	conflicting.Objective = "different"
	if _, err = repo.Generate(ctx, workspace, conflicting); err == nil {
		t.Fatal("generation idempotency conflict accepted")
	}
	bundle, err := repo.Execution(ctx, workspace, command.JobID)
	if err != nil {
		t.Fatal(err)
	}
	if bundle.Snapshot["workflow"] != "content.generate@1" {
		t.Fatal("generation snapshot was not frozen")
	}
	var request GenerationRequest
	_ = json.Unmarshal(command.Payload, &request)
	version := uuid.NewString()
	body, _ := json.Marshal(map[string]any{"document_id": request.DocumentID, "project_id": project.ID, "document_type": "screenplay", "version": 1, "schema_version": "1.0", "content_hash": "ir-hash", "blocks": []map[string]any{{"type": "scene", "text": "Scene text", "position": 0, "metadata": map[string]any{}}}})
	repo.WithContentArtifacts(generatedArtifact{body: body})
	key, _ := objectstorage.ObjectKey(workspace, project.ID, "documents", request.DocumentID, "content.json", version)
	commit := map[string]any{"document_id": request.DocumentID, "document_version_id": version, "source_version": 1, "ir_hash": "ir-hash", "artifact": map[string]any{"object_key": key, "sha256": "checksum"}}
	payload, _ := json.Marshal(commit)
	completed := contracts.Event{SchemaVersion: "1.0", ID: uuid.NewString(), Type: "job.completed", WorkspaceID: workspace, ProjectID: project.ID, JobID: command.JobID, WorkflowRunID: command.WorkflowRunID, Sequence: 1, Payload: json.RawMessage(`{}`)}
	completedJSON, _ := json.Marshal(completed)
	if err = repo.Project(ctx, completedJSON); err != nil {
		t.Fatal(err)
	}
	event := contracts.Event{SchemaVersion: "1.0", ID: uuid.NewString(), Type: "content.version.committed", WorkspaceID: workspace, ProjectID: project.ID, JobID: command.JobID, WorkflowRunID: command.WorkflowRunID, Sequence: 2, Payload: payload}
	data, _ := json.Marshal(event)
	if err = repo.Project(ctx, data); err != nil {
		t.Fatal(err)
	}
	if err = repo.Project(ctx, data); err != nil {
		t.Fatal(err)
	}
	stored, err := repo.ContentVersion(ctx, workspace, project.ID, version)
	if err != nil {
		t.Fatal(err)
	}
	if stored.Document.ProjectID != project.ID || len(stored.Document.Blocks) != 1 {
		t.Fatal("IR projection failed")
	}
	if _, err = repo.ContentVersion(ctx, uuid.NewString(), project.ID, version); err == nil {
		t.Fatal("cross workspace content visible")
	}
}
