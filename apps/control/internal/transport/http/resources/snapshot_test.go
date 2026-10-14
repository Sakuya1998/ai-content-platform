package resources

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	contentpostgres "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/content/postgres"
	executionpostgres "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/execution/postgres"
	identitypostgres "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity/postgres"
	projectpostgres "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project/postgres"
	projecttransfer "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project/transfer"
	transferpostgres "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project/transfer/postgres"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/task"
	taskpostgres "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/task/postgres"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/objectstorage"
)

var loadResource = projectpostgres.Load
var readResourceEnvelope = executionpostgres.ReadEnvelope

func NewResourceAPI(pool *pgxpool.Pool, messages *executionpostgres.MQRepository, reader ArtifactReader) *ResourceAPI {
	projects := projectpostgres.Repository{Pool: pool, Conflict: ErrMessageConflict, Audit: identitypostgres.RecordProjectAudit, MarkCandidatesStale: executionpostgres.MarkCandidatesStale, CreateInitialDocument: contentpostgres.CreateDocumentWithin}
	content := contentpostgres.Repository{Pool: pool, EnsureIdle: projectpostgres.EnsureIdle, Conflict: ErrMessageConflict, Invalid: errResourceInvalid}
	transfers := transferpostgres.Repository{
		Pool: pool, Conflict: ErrMessageConflict, Invalid: errResourceInvalid,
		LoadProject: projectpostgres.Load, CreateProjectWithin: projects.CreateWithin, Audit: identitypostgres.RecordProjectAudit,
		ReadRecords: executionpostgres.ReadRecords, PersistEnvelope: executionpostgres.PersistEnvelope,
		WriteContentRecord: contentpostgres.PersistContentRecord,
	}
	tasks := taskpostgres.Adapter{Pool: pool, Outbox: messages, Conflict: ErrMessageConflict, Invalid: errResourceInvalid, LoadProject: projectpostgres.Load, NextDocumentVersion: contentpostgres.NextVersionWithin, Audit: identitypostgres.RecordProjectAudit, EnsureIdle: projectpostgres.EnsureIdle, Validate: taskpostgres.OperationValidator(executionpostgres.CandidateIsStale), Envelope: executionpostgres.ReadEnvelope, Snapshot: task.ResourceSnapshot}
	if messages != nil {
		tasks.AttachSnapshot = messages.AttachExecutionSnapshot
	}
	return NewHandler(Dependencies{Projects: projects, Content: content, Tasks: tasks, Records: executionpostgres.Repository{Pool: pool, Audit: identitypostgres.RecordProjectAudit}, Transfers: transfers, Exports: projecttransfer.NewService(transfers, ""), Messaging: messages, SubmitCommand: tasks.SubmitCommand, NormalizeError: func(err error) error {
		var pgerr *pgconn.PgError
		if errors.Is(err, pgx.ErrNoRows) {
			return ErrNotFound
		}
		if errors.As(err, &pgerr) && pgerr.Code == "23505" {
			return ErrMessageConflict
		}
		return err
	}})
}

func (a *ResourceAPI) within(ctx context.Context, scope string, fn func(pgx.Tx) error) error {
	return a.projects.(projectpostgres.Repository).Within(ctx, scope, fn)
}

func (a *ResourceAPI) configureTransferEncryptionKey(key string) {
	a.exports = projecttransfer.NewService(a.transfers, key)
}
func (a *ResourceAPI) encodePendingTransfer(backup ResourceBackup, metadata map[string]string, password string) ([]byte, error) {
	return a.exports.EncodePending(backup, metadata, password)
}

type executionSnapshotWriterStub struct{ body []byte }

type mutableArtifactReader struct{ reader ArtifactReader }

func (r *mutableArtifactReader) Get(ctx context.Context, key, hash string) ([]byte, error) {
	return r.reader.Get(ctx, key, hash)
}

func (s *executionSnapshotWriterStub) Put(_ context.Context, key string, body []byte, contentType string) (objectstorage.ArtifactReference, error) {
	s.body = append([]byte(nil), body...)
	sum := sha256.Sum256(body)
	return objectstorage.ArtifactReference{Bucket: "agent-artifacts", ObjectKey: key, SHA256: hex.EncodeToString(sum[:]), SizeBytes: int64(len(body)), ContentType: contentType}, nil
}
