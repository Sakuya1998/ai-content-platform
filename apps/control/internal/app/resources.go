package app

import (
	"github.com/jackc/pgx/v5/pgxpool"
	contentpostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/content/postgres"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/execution"
	executionpostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/execution/postgres"
	identitypostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/identity/postgres"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/project"
	projectpostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/project/postgres"
	projecttransfer "github.com/sakuya/novel-agent/apps/control/internal/modules/project/transfer"
	transferpostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/project/transfer/postgres"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/task"
	taskpostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/task/postgres"
	"github.com/sakuya/novel-agent/apps/control/internal/transport/http/resources"
)

type resourceOptions struct {
	SnapshotFreezer, InputFreezer execution.SnapshotFreezer
	TransferEncryptionKey         string
	ArtifactMaxBytes              int64
}

func newResourceHandler(pool *pgxpool.Pool, messages *executionpostgres.MQRepository, reader execution.ArtifactReader, options resourceOptions) *resources.ResourceAPI {
	projects := projectpostgres.Repository{Pool: pool, Conflict: task.ErrConflict, Audit: identitypostgres.RecordProjectAudit, MarkCandidatesStale: executionpostgres.MarkCandidatesStale, CreateInitialDocument: contentpostgres.CreateDocumentWithin}
	content := contentpostgres.Repository{Pool: pool, EnsureIdle: projectpostgres.EnsureIdle, Conflict: task.ErrConflict, Invalid: project.ErrInvalid}
	transfers := transferpostgres.Repository{
		Pool: pool, Conflict: task.ErrConflict, Invalid: project.ErrInvalid,
		LoadProject: projectpostgres.Load, CreateProjectWithin: projects.CreateWithin, Audit: identitypostgres.RecordProjectAudit,
		ReadRecords: executionpostgres.ReadRecords, PersistEnvelope: executionpostgres.PersistEnvelope,
		WriteContentRecord: contentpostgres.PersistContentRecord,
	}
	tasks := taskpostgres.Adapter{Pool: pool, Outbox: messages, Conflict: task.ErrConflict, Invalid: task.ErrInvalid, LoadProject: projectpostgres.Load, NextDocumentVersion: contentpostgres.NextVersionWithin, Audit: identitypostgres.RecordProjectAudit, EnsureIdle: projectpostgres.EnsureIdle, Validate: taskpostgres.OperationValidator(executionpostgres.CandidateIsStale), Envelope: executionpostgres.ReadEnvelope, Freezer: options.SnapshotFreezer, InputFreezer: options.InputFreezer, Snapshot: task.ResourceSnapshot}
	if messages != nil {
		tasks.AttachSnapshot = messages.AttachExecutionSnapshot
	}
	return resources.NewHandler(resources.Dependencies{Projects: projects, Content: content, Tasks: tasks, Records: executionpostgres.Repository{Pool: pool, Audit: identitypostgres.RecordProjectAudit}, Transfers: transfers, Exports: projecttransfer.NewService(transfers, options.TransferEncryptionKey), Messaging: messages, SubmitCommand: tasks.SubmitCommand, NormalizeError: normalizeResourceError})
}
