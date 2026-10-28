package postgres

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/evaluation"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/database"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type AuditWriter func(context.Context, pgx.Tx, identity.Principal, string, string, string) error

type Repository struct {
	pool  *pgxpool.Pool
	audit AuditWriter
}

func NewRepository(pool *pgxpool.Pool, audit ...AuditWriter) *Repository {
	repository := &Repository{pool: pool}
	if len(audit) > 0 {
		repository.audit = audit[0]
	}
	return repository
}

func (r *Repository) Benchmark(ctx context.Context, workspace, id string) (json.RawMessage, error) {
	var report json.RawMessage
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT report FROM evaluation_benchmark_runs WHERE id::text=$1 AND workspace_id=$2", id, workspace).Scan(&report)
	})
	if errors.Is(err, pgx.ErrNoRows) {
		err = project.ErrNotFound
	}
	return report, err
}

func (r *Repository) Benchmarks(ctx context.Context, workspace string) ([]json.RawMessage, error) {
	return r.BenchmarksFiltered(ctx, workspace, evaluation.BenchmarkFilters{})
}

func (r *Repository) BenchmarksFiltered(ctx context.Context, workspace string, filters evaluation.BenchmarkFilters) ([]json.RawMessage, error) {
	items := []json.RawMessage{}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `SELECT report FROM evaluation_benchmark_runs
			WHERE workspace_id=$1 AND ($2='' OR coalesce(job_id,id::text)=$2)
			AND ($3='' OR status=$3 OR report->>'status'=$3)
			ORDER BY created_at DESC,id LIMIT 100`,
			workspace, filters.JobID, filters.Status,
		)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var report json.RawMessage
			if err = rows.Scan(&report); err != nil {
				return err
			}
			items = append(items, report)
		}
		return rows.Err()
	})
	return items, err
}

func RecordBenchmark(ctx context.Context, tx pgx.Tx, workspace, id string, report json.RawMessage) error {
	metadata, err := benchmarkMetadata(id, report)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO evaluation_benchmark_runs
		(id,workspace_id,job_id,policy_version,overall_score,status,report)
		VALUES($1,$2,$3,$4,$5,'completed',$6) ON CONFLICT(id) DO NOTHING`,
		id, workspace, id, nullableString(metadata.policyVersion), nullableScore(metadata.overallScore), report,
	)
	return err
}

func ReadBenchmarkWithin(ctx context.Context, tx pgx.Tx, workspace, id string) (json.RawMessage, error) {
	var report json.RawMessage
	err := tx.QueryRow(ctx, "SELECT report FROM evaluation_benchmark_runs WHERE id::text=$1 AND workspace_id=$2", id, workspace).Scan(&report)
	if errors.Is(err, pgx.ErrNoRows) {
		err = project.ErrNotFound
	}
	return report, err
}

func ResolveBenchmarkReport(ctx context.Context, tx pgx.Tx, workspace, id string) (json.RawMessage, error) {
	return ReadBenchmarkWithin(ctx, tx, workspace, id)
}

func (r *Repository) within(ctx context.Context, workspace string, fn func(pgx.Tx) error) error {
	if r.pool == nil {
		return evaluation.ErrUnavailable
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.pool, workspace)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	return fn(tx)
}
