package postgres

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"testing"

	"github.com/sakuya/novel-agent/go-control-plane/internal/contracts"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/config"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/objectstorage"
)

type executionSnapshotWriterStub struct {
	key         string
	body        []byte
	contentType string
}

func TestPublishedExecutionCommandContainsOnlySnapshotReference(t *testing.T) {
	command := contracts.Command{Type: "workflow.run", Payload: json.RawMessage(`{"objective":"private manuscript"}`),
		ExecutionSnapshot: &contracts.ExecutionSnapshotReference{ObjectKey: "reference", SHA256: strings.Repeat("a", 64), SizeBytes: 10}}
	body, err := commandMessage(command)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(body), "private manuscript") {
		t.Fatal("full execution request was published to MQ")
	}
	var published contracts.Command
	if err := json.Unmarshal(body, &published); err != nil {
		t.Fatal(err)
	}
	if string(published.Payload) != "{}" || published.ExecutionSnapshot == nil {
		t.Fatal("missing frozen snapshot reference")
	}
}

func (w *executionSnapshotWriterStub) Put(_ context.Context, key string, body []byte, contentType string) (objectstorage.ArtifactReference, error) {
	w.key = key
	w.body = append([]byte(nil), body...)
	w.contentType = contentType
	digest := sha256.Sum256(body)
	return objectstorage.ArtifactReference{
		Bucket:      "agent-artifacts",
		ObjectKey:   key,
		SHA256:      hex.EncodeToString(digest[:]),
		SizeBytes:   int64(len(body)),
		ContentType: contentType,
	}, nil
}

func TestWriteExecutionSnapshotStoresScopedImmutableObject(t *testing.T) {
	writer := &executionSnapshotWriterStub{}
	repo := newTestMQRepository(nil, config.MQTopics{}).WithExecutionSnapshotStore(writer)
	capabilityHash := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	bundle := ExecutionBundle{
		SchemaVersion:     "1.0",
		WorkspaceID:       "workspace-1",
		ProjectID:         "project-1",
		JobID:             "job-1",
		Snapshot:          map[string]any{"workflow": "content.generate@1", "snapshot_hash": capabilityHash},
		Request:           GenerationRequest{ProjectID: "project-1", DocumentType: "screenplay", Title: "Scene", Objective: "Write a scene"},
		Source:            nil,
		Inputs:            []ContentVersion{},
		ResolvedResources: map[string]any{},
	}

	reference, err := repo.writeExecutionSnapshot(context.Background(), bundle)
	if err != nil {
		t.Fatal(err)
	}
	if writer.contentType != "application/json" || reference.SizeBytes != int64(len(writer.body)) {
		t.Fatalf("snapshot object metadata mismatch: %#v", reference)
	}
	storedDigest := sha256.Sum256(writer.body)
	if reference.SHA256 != hex.EncodeToString(storedDigest[:]) {
		t.Fatal("snapshot reference checksum does not match stored bytes")
	}
	expectedKey, err := objectstorage.ObjectKey("workspace-1", "project-1", "assets", "job-1", "execution.json", reference.SHA256)
	if err != nil || writer.key != expectedKey || reference.ObjectKey != expectedKey {
		t.Fatalf("snapshot object key = %q, expected %q (error %v)", writer.key, expectedKey, err)
	}

	var snapshot contracts.ExecutionSnapshot
	if err := json.Unmarshal(writer.body, &snapshot); err != nil {
		t.Fatal(err)
	}
	command := contracts.Command{
		WorkspaceID:            bundle.WorkspaceID,
		ProjectID:              bundle.ProjectID,
		JobID:                  bundle.JobID,
		CapabilitySnapshotHash: capabilityHash,
		ExecutionSnapshot:      reference,
	}
	if err := snapshot.ValidateFor(command); err != nil {
		t.Fatalf("stored snapshot does not satisfy command contract: %v", err)
	}
}

func TestWriteExecutionSnapshotRequiresObjectStorage(t *testing.T) {
	repo := newTestMQRepository(nil, config.MQTopics{})
	if reference, err := repo.writeExecutionSnapshot(context.Background(), ExecutionBundle{}); err == nil || reference != nil {
		t.Fatal("execution accepted without immutable object storage")
	}
}
