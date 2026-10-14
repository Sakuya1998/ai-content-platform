package postgres

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/sakuya/novel-agent/go-control-plane/internal/contracts"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/execution"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/task"
)

type ProjectionAdapter struct {
	tx           pgx.Tx
	writeContent ContentRecordWriter
}

func (p ProjectionAdapter) Record(ctx context.Context, event contracts.Event, kind string, row map[string]any) error {
	return InsertRecord(ctx, p.tx, event.WorkspaceID, event.ProjectID, event.JobID, kind, row, execution.ErrProjectionConflict, task.ErrConflict, p.writeContent)
}

func (p ProjectionAdapter) Apply(ctx context.Context, event contracts.Event, stateHash string, envelope execution.Envelope) error {
	var projectID string
	if err := p.tx.QueryRow(ctx, "SELECT id FROM project_projections WHERE workspace_id=$1 AND id=$2 FOR UPDATE", event.WorkspaceID, event.ProjectID).Scan(&projectID); err != nil {
		return err
	}
	var jobProject string
	if err := p.tx.QueryRow(ctx, "SELECT project_id FROM jobs WHERE workspace_id=$1 AND id::text=$2", event.WorkspaceID, event.JobID).Scan(&jobProject); err != nil {
		return err
	}
	if jobProject != projectID {
		return task.ErrConflict
	}
	return PersistEnvelope(ctx, p.tx, event.WorkspaceID, projectID, event.JobID, stateHash, envelope, execution.ErrProjectionConflict, task.ErrConflict, p.writeContent)
}

func NewEventProjector(reader execution.ArtifactReader, maxBytes int64, contentWriters ...ContentRecordWriter) func(context.Context, pgx.Tx, contracts.Event) error {
	if maxBytes <= 0 {
		maxBytes = 50 << 20
	}
	var writeContent ContentRecordWriter
	if len(contentWriters) > 0 {
		writeContent = contentWriters[0]
	}
	return func(ctx context.Context, tx pgx.Tx, event contracts.Event) error {
		err := (execution.Projector{Reader: reader, Store: ProjectionAdapter{tx: tx, writeContent: writeContent}, MaxBytes: maxBytes}).Project(ctx, event)
		if errors.Is(err, execution.ErrProjectionConflict) {
			return task.ErrConflict
		}
		return err
	}
}
