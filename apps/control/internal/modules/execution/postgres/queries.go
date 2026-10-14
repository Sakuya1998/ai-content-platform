package postgres

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/execution"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/project"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/database"
)

type Repository struct {
	Pool  *pgxpool.Pool
	Audit func(context.Context, pgx.Tx, string, string, string) error
}

func (r Repository) within(ctx context.Context, scope string, fn func(pgx.Tx) error) error {
	if r.Pool == nil {
		return project.ErrNotFound
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.Pool, scope)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (r Repository) Records(ctx context.Context, scope, id, kind string, index int, artifact string) ([]map[string]any, error) {
	var records []map[string]any
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		var err error
		records, err = ReadRecords(ctx, tx, scope, id, kind, index, artifact)
		return err
	})
	return records, err
}

func ReadEnvelope(ctx context.Context, tx pgx.Tx, scope string, resource project.Resource) (execution.Envelope, error) {
	envelope := execution.Envelope{State: resource.State}
	targets := map[string]*[]map[string]any{"content": &envelope.ContentVersions, "planning": &envelope.PlanningVersions, "candidate": &envelope.Candidates, "evaluation": &envelope.Evaluations, "audit": &envelope.Audits, "memory": &envelope.MemorySnapshots, "memory_quality": &envelope.MemoryQuality, "usage": &envelope.Usage, "trace": &envelope.Traces}
	for kind, target := range targets {
		records, err := ReadRecords(ctx, tx, scope, resource.ID, kind, 0, "")
		if err != nil {
			return envelope, err
		}
		for _, record := range records {
			delete(record, "record_id")
			delete(record, "record_key")
		}
		*target = records
	}
	return envelope, nil
}

func (r Repository) Diff(ctx context.Context, scope, id, kind string, index, from, to int, artifact string) (map[string]any, map[string]any, error) {
	var before, after map[string]any
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		var err error
		before, err = FindVersion(ctx, tx, scope, id, kind, index, from, artifact)
		if err != nil {
			return err
		}
		after, err = FindVersion(ctx, tx, scope, id, kind, index, to, artifact)
		return err
	})
	return before, after, err
}
func (r Repository) Version(ctx context.Context, scope, id, kind string, index, version int, artifact string) (map[string]any, error) {
	var record map[string]any
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		var err error
		record, err = FindVersion(ctx, tx, scope, id, kind, index, version, artifact)
		return err
	})
	return record, err
}
func (r Repository) Baseline(ctx context.Context, scope, id string, index int, key string) error {
	return r.within(ctx, scope, func(tx pgx.Tx) error {
		var locked string
		if err := tx.QueryRow(ctx, "SELECT id FROM project_projections WHERE workspace_id=$1 AND id=$2 FOR UPDATE", scope, id).Scan(&locked); err != nil {
			return err
		}
		var record string
		if err := tx.QueryRow(ctx, "SELECT id FROM project_records WHERE workspace_id=$1 AND project_id=$2 AND kind='evaluation' AND block_index=$3 AND (record_key=$4 OR id::text=$4)", scope, id, index, key).Scan(&record); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, "UPDATE project_records SET baseline=false WHERE workspace_id=$1 AND project_id=$2 AND kind='evaluation' AND block_index=$3", scope, id, index); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, "UPDATE project_records SET baseline=true WHERE workspace_id=$1 AND id=$2", scope, record); err != nil {
			return err
		}
		if r.Audit == nil {
			return errors.New("audit writer unavailable")
		}
		return r.Audit(ctx, tx, scope, "project.evaluation.baseline", id)
	})
}
