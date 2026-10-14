package postgres

import (
	"context"
	"encoding/json"
	"os"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/contracts"
	projectmodule "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/config"
)

func TestMQCommandOutboxAndOrderedProjection(t *testing.T) {
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
	if _, err = pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'MQ test')", workspace); err != nil {
		t.Fatal(err)
	}
	project, err := createProjectFixture(pool, projectmodule.Project{WorkspaceID: workspace, Name: "MQ test", ProjectType: "generic"})
	if err != nil {
		t.Fatal(err)
	}
	repo := newTestMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"})
	command := contracts.Command{SchemaVersion: "1.0", ID: uuid.NewString(), Type: "workflow.run", WorkspaceID: workspace, ProjectID: project.ID, JobID: uuid.NewString(), WorkflowRunID: uuid.NewString(), CapabilitySnapshotHash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", TraceID: "trace", IdempotencyKey: uuid.NewString(), Payload: json.RawMessage(`{}`)}
	command.ExecutionSnapshot = &contracts.ExecutionSnapshotReference{
		ObjectKey: "workspaces/" + workspace + "/projects/" + project.ID + "/assets/" + command.JobID + "/versions/v1/execution.json",
		SHA256:    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", SizeBytes: 128,
	}
	if err = repo.Submit(ctx, command); err != nil {
		t.Fatal(err)
	}
	if err = repo.Submit(ctx, command); err != nil {
		t.Fatal(err)
	}
	count := 0
	if err = repo.Relay(ctx, workspace, func(context.Context, string, string, string, []byte) error { count++; return nil }); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("expected one command, got %d", count)
	}
	wrongRun := contracts.Event{SchemaVersion: "1.0", ID: uuid.NewString(), Type: "job.running", WorkspaceID: workspace, ProjectID: project.ID, JobID: command.JobID, WorkflowRunID: uuid.NewString(), Sequence: 1, Payload: json.RawMessage(`{}`)}
	wrongBody, _ := json.Marshal(wrongRun)
	if err = repo.Project(ctx, wrongBody); err == nil {
		t.Fatal("event for another workflow run was accepted")
	}
	event := contracts.Event{SchemaVersion: "1.0", ID: uuid.NewString(), Type: "job.completed", WorkspaceID: workspace, ProjectID: project.ID, JobID: command.JobID, WorkflowRunID: command.WorkflowRunID, OperationID: "op", TraceID: "trace", Sequence: 2, Payload: json.RawMessage(`{"ok":true}`)}
	second, _ := json.Marshal(event)
	if err = repo.Project(ctx, second); err != nil {
		t.Fatal(err)
	}
	job, err := repo.Get(ctx, workspace, command.JobID)
	if err != nil || job.Status != "queued" {
		t.Fatalf("out of order event applied: %#v %v", job, err)
	}
	event.ID = uuid.NewString()
	event.Type = "job.running"
	event.Sequence = 1
	first, _ := json.Marshal(event)
	if err = repo.Project(ctx, first); err != nil {
		t.Fatal(err)
	}
	if err = repo.Project(ctx, second); err != nil {
		t.Fatal(err)
	}
	job, err = repo.Get(ctx, workspace, command.JobID)
	if err != nil || job.Status != "completed" || job.Sequence != 2 {
		t.Fatalf("projection: %#v %v", job, err)
	}
	event.Payload = json.RawMessage(`{"changed":true}`)
	conflict, _ := json.Marshal(event)
	if err = repo.Project(ctx, conflict); err == nil {
		t.Fatal("event ID conflict accepted")
	}
	if _, err = repo.Get(ctx, uuid.NewString(), command.JobID); err == nil {
		t.Fatal("cross workspace job visible")
	}
}

func TestProjectionPreservesRequestedCancellation(t *testing.T) {
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
	if _, err = pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Cancel test')", workspace); err != nil {
		t.Fatal(err)
	}
	project, err := createProjectFixture(pool, projectmodule.Project{WorkspaceID: workspace, Name: "Cancel test", ProjectType: "generic"})
	if err != nil {
		t.Fatal(err)
	}
	repo := newTestMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"})
	command := contracts.Command{SchemaVersion: "1.0", ID: uuid.NewString(), Type: "workflow.run", WorkspaceID: workspace, ProjectID: project.ID, JobID: uuid.NewString(), WorkflowRunID: uuid.NewString(), IdempotencyKey: uuid.NewString(), Payload: json.RawMessage(`{}`)}
	if err = repo.Submit(ctx, command); err != nil {
		t.Fatal(err)
	}
	command.ID = uuid.NewString()
	command.Type = "job.cancel"
	command.IdempotencyKey = command.ID
	if err = repo.Submit(ctx, command); err != nil {
		t.Fatal(err)
	}
	event := contracts.Event{ID: uuid.NewString(), SchemaVersion: "1.0", Type: "job.running", WorkspaceID: workspace, ProjectID: project.ID, JobID: command.JobID, WorkflowRunID: command.WorkflowRunID, Sequence: 1, Payload: json.RawMessage(`{}`)}
	body, _ := json.Marshal(event)
	if err = repo.Project(ctx, body); err != nil {
		t.Fatal(err)
	}
	job, err := repo.Get(ctx, workspace, command.JobID)
	if err != nil || job.Status != "cancel_requested" {
		t.Fatalf("late running event revoked cancellation: %#v %v", job, err)
	}
	event.ID = uuid.NewString()
	event.Type = "job.cancelled"
	event.Sequence = 2
	body, _ = json.Marshal(event)
	if err = repo.Project(ctx, body); err != nil {
		t.Fatal(err)
	}
	event.ID = uuid.NewString()
	event.Type = "job.completed"
	event.Sequence = 3
	event.Payload = json.RawMessage(`{"stale":true}`)
	body, _ = json.Marshal(event)
	if err = repo.Project(ctx, body); err != nil {
		t.Fatal(err)
	}
	job, err = repo.Get(ctx, workspace, command.JobID)
	if err != nil || job.Status != "cancelled" || string(job.Result) != "{}" {
		t.Fatalf("stale completion overwrote cancelled outcome: %#v %v", job, err)
	}
	projected := 0
	repo.WithProjectProjector(func(context.Context, pgx.Tx, contracts.Event) error { projected++; return nil })
	event.ID, event.Type, event.Sequence = uuid.NewString(), "project.state.changed", 4
	body, _ = json.Marshal(event)
	if err = repo.Project(ctx, body); err != nil {
		t.Fatal(err)
	}
	if projected != 0 {
		t.Fatal("late project state mutated a cancelled execution")
	}
	event.ID, event.Type, event.Sequence = uuid.NewString(), "project.usage.recorded", 5
	body, _ = json.Marshal(event)
	if err = repo.Project(ctx, body); err != nil {
		t.Fatal(err)
	}
	if projected != 1 {
		t.Fatal("terminal execution telemetry was dropped")
	}
}

func TestProjectionPreservesVideoReviewAndCancellationProvenance(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	workspace := uuid.NewString()
	if _, err := pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Video cancellation projection')", workspace); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = pool.Exec(ctx, "DELETE FROM workspaces WHERE id=$1", workspace) })
	project, err := createProjectFixture(pool, projectmodule.Project{WorkspaceID: workspace, Name: "Video cancellation", ProjectType: "video"})
	if err != nil {
		t.Fatal(err)
	}
	repo := newTestMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"})
	command := contracts.Command{SchemaVersion: "1.0", ID: uuid.NewString(), Type: "workflow.run", WorkspaceID: workspace, ProjectID: project.ID, JobID: uuid.NewString(), WorkflowRunID: uuid.NewString(), IdempotencyKey: uuid.NewString(), Payload: json.RawMessage(`{}`)}
	if err = repo.Submit(ctx, command); err != nil {
		t.Fatal(err)
	}
	review := json.RawMessage(`{"media_status":"running","remote_task_id":"original-task","review":{"approved":false,"notes":["Video is still running"]}}`)
	pending := json.RawMessage(`{"media_status":"cancel_pending","remote_task_id":"original-task","remote_cancel_confirmed":false,"remote_work_terminal":false}`)
	for index, step := range []struct {
		kind    string
		payload json.RawMessage
		status  string
		result  json.RawMessage
	}{
		{"job.running", json.RawMessage(`{}`), "running", json.RawMessage(`{}`)},
		{"job.waiting_review", review, "waiting_review", review},
		{"job.accepted", json.RawMessage(`{}`), "queued", review},
		{"job.running", json.RawMessage(`{}`), "running", review},
		{"job.cancel_requested", pending, "cancel_requested", pending},
		{"job.running", json.RawMessage(`{}`), "cancel_requested", pending},
		{"job.cancelled", pending, "cancelled", pending},
		{"job.completed", json.RawMessage(`{"remote_cancel_confirmed":true,"remote_work_terminal":true}`), "cancelled", pending},
	} {
		event := contracts.Event{SchemaVersion: "1.0", ID: uuid.NewString(), Type: step.kind, WorkspaceID: workspace, ProjectID: project.ID, JobID: command.JobID, WorkflowRunID: command.WorkflowRunID, Sequence: int64(index + 1), Payload: step.payload}
		body, _ := json.Marshal(event)
		if err = repo.Project(ctx, body); err != nil {
			t.Fatalf("project %s: %v", step.kind, err)
		}
		if err = repo.Project(ctx, body); err != nil {
			t.Fatalf("replay %s: %v", step.kind, err)
		}
		job, err := repo.Get(ctx, workspace, command.JobID)
		if err != nil || job.Status != step.status || !sameJSON(job.Result, step.result) || job.Sequence != int64(index+1) {
			t.Fatalf("%s lost review or cancellation provenance: job=%#v error=%v want=%s", step.kind, job, err, step.result)
		}
	}
}
