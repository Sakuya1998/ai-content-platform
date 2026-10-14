package postgres

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/project"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/database"
)

type Repository struct{ pool *pgxpool.Pool }

func NewRepository(pool *pgxpool.Pool) *Repository { return &Repository{pool: pool} }

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
	items := []json.RawMessage{}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, "SELECT report FROM evaluation_benchmark_runs WHERE workspace_id=$1 ORDER BY created_at DESC,id LIMIT 100", workspace)
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
	_, err := tx.Exec(ctx, "INSERT INTO evaluation_benchmark_runs(id,workspace_id,report) VALUES($1,$2,$3) ON CONFLICT(id) DO NOTHING", id, workspace, report)
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
		return errors.New("evaluation store unavailable")
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.pool, workspace)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	return fn(tx)
}
