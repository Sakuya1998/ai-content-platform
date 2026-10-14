package contracts

import (
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
)

type ExecutionSnapshotReference struct {
	ObjectKey string `json:"object_key"`
	SHA256    string `json:"sha256"`
	SizeBytes int64  `json:"size_bytes"`
}

type ExecutionSnapshot struct {
	SchemaVersion     string            `json:"schema_version"`
	WorkspaceID       string            `json:"workspace_id"`
	ProjectID         string            `json:"project_id"`
	JobID             string            `json:"job_id"`
	Snapshot          json.RawMessage   `json:"snapshot"`
	Request           json.RawMessage   `json:"request"`
	Source            json.RawMessage   `json:"source"`
	Inputs            []json.RawMessage `json:"inputs"`
	ResolvedResources json.RawMessage   `json:"resolved_resources"`
	CommittedVersion  json.RawMessage   `json:"committed_version,omitempty"`
}

func (s ExecutionSnapshot) ValidateFor(command Command) error {
	if s.SchemaVersion != "1.0" {
		return errors.New("unsupported execution snapshot schema version")
	}
	if s.WorkspaceID != command.WorkspaceID || s.ProjectID != command.ProjectID || s.JobID != command.JobID {
		return errors.New("execution snapshot identity conflict")
	}
	if command.ExecutionSnapshot == nil {
		return errors.New("execution snapshot reference is required")
	}
	if err := command.ExecutionSnapshot.validateScope(command.WorkspaceID, command.ProjectID); err != nil {
		return err
	}
	if !jsonObject(s.Snapshot) || !jsonObject(s.Request) || !jsonObject(s.ResolvedResources) || s.Inputs == nil {
		return errors.New("execution snapshot body is incomplete")
	}
	var capability struct {
		SnapshotHash string `json:"snapshot_hash"`
	}
	if err := json.Unmarshal(s.Snapshot, &capability); err != nil {
		return err
	}
	if capability.SnapshotHash != command.CapabilitySnapshotHash {
		return errors.New("capability snapshot checksum conflict")
	}
	var request struct {
		ProjectID string `json:"project_id"`
	}
	if err := json.Unmarshal(s.Request, &request); err != nil {
		return err
	}
	if request.ProjectID != "" && request.ProjectID != s.ProjectID {
		return errors.New("execution snapshot request project conflict")
	}
	return nil
}

func (r ExecutionSnapshotReference) validateScope(workspace, project string) error {
	if r.SizeBytes < 1 || len(r.SHA256) != 64 || r.SHA256 != strings.ToLower(r.SHA256) {
		return errors.New("invalid execution snapshot reference")
	}
	if _, err := hex.DecodeString(r.SHA256); err != nil {
		return errors.New("invalid execution snapshot checksum")
	}
	prefix := fmt.Sprintf("workspaces/%s/projects/%s/", workspace, project)
	if !strings.HasPrefix(r.ObjectKey, prefix) || len(r.ObjectKey) > 1024 {
		return errors.New("execution snapshot object key must match command scope")
	}
	for _, segment := range strings.Split(r.ObjectKey, "/") {
		if segment == "" || segment == "." || segment == ".." {
			return errors.New("invalid execution snapshot object key")
		}
	}
	return nil
}

func jsonObject(value json.RawMessage) bool {
	var object map[string]json.RawMessage
	return json.Unmarshal(value, &object) == nil && object != nil
}

type Command struct {
	SchemaVersion          string                      `json:"schema_version"`
	OccurredAt             *string                     `json:"occurred_at"`
	CausationID            *string                     `json:"causation_id"`
	ID                     string                      `json:"command_id"`
	Type                   string                      `json:"command_type"`
	WorkspaceID            string                      `json:"workspace_id"`
	ProjectID              string                      `json:"project_id"`
	JobID                  string                      `json:"job_id"`
	WorkflowRunID          string                      `json:"workflow_run_id"`
	CapabilitySnapshotHash string                      `json:"capability_snapshot_hash"`
	ExecutionSnapshot      *ExecutionSnapshotReference `json:"execution_snapshot,omitempty"`
	TraceID                string                      `json:"trace_id"`
	IdempotencyKey         string                      `json:"idempotency_key"`
	Payload                json.RawMessage             `json:"payload"`
}

type Event struct {
	SchemaVersion string          `json:"schema_version"`
	OccurredAt    *string         `json:"occurred_at"`
	CausationID   *string         `json:"causation_id"`
	ID            string          `json:"event_id"`
	Type          string          `json:"event_type"`
	WorkspaceID   string          `json:"workspace_id"`
	ProjectID     string          `json:"project_id"`
	JobID         string          `json:"job_id"`
	WorkflowRunID string          `json:"workflow_run_id"`
	OperationID   string          `json:"operation_id"`
	TraceID       string          `json:"trace_id"`
	Sequence      int64           `json:"sequence"`
	Payload       json.RawMessage `json:"payload"`
}
