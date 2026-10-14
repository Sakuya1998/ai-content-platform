package execution

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"

	"github.com/sakuya/novel-agent/go-control-plane/internal/contracts"
)

var ErrProjectionConflict = errors.New("execution projection scope or hash conflict")

type ArtifactReader interface {
	Get(context.Context, string, string) ([]byte, error)
}
type ProjectionStore interface {
	Record(context.Context, contracts.Event, string, map[string]any) error
	Apply(context.Context, contracts.Event, string, Envelope) error
}

type Projector struct {
	Reader   ArtifactReader
	Store    ProjectionStore
	MaxBytes int64
}

func (p Projector) Project(ctx context.Context, event contracts.Event) error {
	if event.Type == "project.usage.recorded" || event.Type == "project.trace.recorded" {
		var row map[string]any
		if err := json.Unmarshal(event.Payload, &row); err != nil {
			return err
		}
		row["event_id"] = event.ID
		kind := "usage"
		if event.Type == "project.trace.recorded" {
			kind = "trace"
		}
		return p.Store.Record(ctx, event, kind, row)
	}
	if event.Type != "project.state.changed" {
		return nil
	}
	var payload struct {
		ProjectID string `json:"project_id"`
		StateHash string `json:"state_hash"`
		Artifact  struct {
			Bucket    string `json:"bucket"`
			ObjectKey string `json:"object_key"`
			SHA256    string `json:"sha256"`
		} `json:"artifact"`
	}
	if err := json.Unmarshal(event.Payload, &payload); err != nil {
		return err
	}
	if payload.ProjectID != "" && payload.ProjectID != event.ProjectID {
		return ErrProjectionConflict
	}
	prefix := "workspaces/" + event.WorkspaceID + "/projects/" + event.ProjectID + "/"
	if payload.Artifact.Bucket != "agent-artifacts" || !SafeObjectKey(payload.Artifact.ObjectKey, prefix) || len(payload.Artifact.SHA256) != 64 || p.Reader == nil {
		return ErrProjectionConflict
	}
	body, err := p.Reader.Get(ctx, payload.Artifact.ObjectKey, payload.Artifact.SHA256)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(body)
	if int64(len(body)) > p.MaxBytes || hex.EncodeToString(sum[:]) != payload.Artifact.SHA256 {
		return ErrProjectionConflict
	}
	var envelope Envelope
	if err = json.Unmarshal(body, &envelope); err != nil {
		return err
	}
	if envelope.State == nil {
		return ErrProjectionConflict
	}
	if stateID, _ := envelope.State["project_id"].(string); stateID != "" && stateID != event.ProjectID {
		return ErrProjectionConflict
	}
	return p.Store.Apply(ctx, event, payload.StateHash, envelope)
}

func SafeObjectKey(key, prefix string) bool {
	if !strings.HasPrefix(key, prefix) || strings.ContainsAny(key, "\\\x00") {
		return false
	}
	for _, part := range strings.Split(key, "/") {
		if part == "" || part == "." || part == ".." {
			return false
		}
	}
	return true
}
