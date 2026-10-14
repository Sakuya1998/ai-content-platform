package contracts

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestCommandRoundTripPreservesExecutionSnapshotReference(t *testing.T) {
	const body = `{
		"schema_version":"1.0",
		"command_id":"command-1",
		"command_type":"workflow.run",
		"workspace_id":"workspace-1",
		"project_id":"project-1",
		"job_id":"job-1",
		"workflow_run_id":"run-1",
		"capability_snapshot_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"trace_id":"trace-1",
		"idempotency_key":"request-1",
		"payload":{},
		"execution_snapshot":{
			"object_key":"workspaces/workspace-1/projects/project-1/assets/job-1/versions/v1/execution.json",
			"sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
			"size_bytes":128
		}
	}`
	var command Command
	if err := json.Unmarshal([]byte(body), &command); err != nil {
		t.Fatalf("unmarshal command: %v", err)
	}

	encoded, err := json.Marshal(command)
	if err != nil {
		t.Fatalf("marshal command: %v", err)
	}
	var roundTrip map[string]any
	if err := json.Unmarshal(encoded, &roundTrip); err != nil {
		t.Fatalf("unmarshal round trip: %v", err)
	}
	reference, ok := roundTrip["execution_snapshot"].(map[string]any)
	if !ok {
		t.Fatal("execution_snapshot reference was dropped")
	}
	if reference["sha256"] != "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" {
		t.Fatalf("unexpected snapshot checksum: %v", reference["sha256"])
	}
}

func TestExecutionSnapshotContractFixtureMatchesCommand(t *testing.T) {
	root := filepath.Join("..", "..", "..", "..", "..", "packages", "contracts", "contracts", "examples")
	commandData, err := os.ReadFile(filepath.Join(root, "command.json"))
	if err != nil {
		t.Fatal(err)
	}
	snapshotData, err := os.ReadFile(filepath.Join(root, "execution-snapshot.json"))
	if err != nil {
		t.Fatal(err)
	}
	var command Command
	if err := json.Unmarshal(commandData, &command); err != nil {
		t.Fatalf("decode command fixture: %v", err)
	}
	var snapshot ExecutionSnapshot
	if err := json.Unmarshal(snapshotData, &snapshot); err != nil {
		t.Fatalf("decode execution snapshot fixture: %v", err)
	}
	if err := snapshot.ValidateFor(command); err != nil {
		t.Fatalf("validate shared execution snapshot fixture: %v", err)
	}
}

func TestExecutionSnapshotRejectsUnknownVersionAndCapabilityHashMismatch(t *testing.T) {
	command := Command{
		SchemaVersion:          "1.0",
		ID:                     "command-1",
		Type:                   "workflow.run",
		WorkspaceID:            "workspace-1",
		ProjectID:              "project-1",
		JobID:                  "job-1",
		WorkflowRunID:          "run-1",
		CapabilitySnapshotHash: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		ExecutionSnapshot: &ExecutionSnapshotReference{
			ObjectKey: "workspaces/workspace-1/projects/project-1/assets/job-1/versions/v1/execution.json",
			SHA256:    "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
			SizeBytes: 128,
		},
	}
	snapshot := ExecutionSnapshot{
		SchemaVersion:     "2.0",
		WorkspaceID:       "workspace-1",
		ProjectID:         "project-1",
		JobID:             "job-1",
		Snapshot:          json.RawMessage(`{"snapshot_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`),
		Request:           json.RawMessage(`{"project_id":"project-1"}`),
		Source:            json.RawMessage(`null`),
		Inputs:            []json.RawMessage{},
		ResolvedResources: json.RawMessage(`{}`),
	}
	if err := snapshot.ValidateFor(command); err == nil {
		t.Fatal("unknown execution snapshot version was accepted")
	}

	snapshot.SchemaVersion = "1.0"
	snapshot.Snapshot = json.RawMessage(`{"snapshot_hash":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"}`)
	if err := snapshot.ValidateFor(command); err == nil {
		t.Fatal("capability snapshot hash mismatch was accepted")
	}
}
