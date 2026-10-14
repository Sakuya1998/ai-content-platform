package postgres

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/database"
)

type Repository struct{ pool *pgxpool.Pool }

func NewRepository(pool *pgxpool.Pool) *Repository { return &Repository{pool: pool} }

func (r *Repository) Latest(ctx context.Context, workspace, projectID, kind string, limit int) (json.RawMessage, error) {
	var data json.RawMessage
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		if err := projectExists(ctx, tx, workspace, projectID); err != nil {
			return err
		}
		if kind == "runtime_usage" {
			var calls, inputs, outputs int64
			var cost string
			if err := tx.QueryRow(ctx, `SELECT count(*),COALESCE(sum((payload->>'input_tokens')::bigint),0),COALESCE(sum((payload->>'output_tokens')::bigint),0),COALESCE(sum((payload->>'cost')::numeric),0)::text FROM project_records WHERE workspace_id=$1 AND project_id=$2 AND kind='usage'`, workspace, projectID).Scan(&calls, &inputs, &outputs, &cost); err != nil {
				return err
			}
			if calls > 0 {
				data, _ = json.Marshal(map[string]any{"calls": calls, "input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs + outputs, "cost": cost})
				return nil
			}
		}
		if kind == "runtime_traces" {
			rows, err := tx.Query(ctx, "SELECT payload FROM project_records WHERE workspace_id=$1 AND project_id=$2 AND kind='trace' ORDER BY created_at DESC,id DESC LIMIT $3", workspace, projectID, limit)
			if err != nil {
				return err
			}
			items := []json.RawMessage{}
			for rows.Next() {
				var item json.RawMessage
				if err = rows.Scan(&item); err != nil {
					rows.Close()
					return err
				}
				items = append(items, item)
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
			if len(items) > 0 {
				data, _ = json.Marshal(map[string]any{"items": items})
				return nil
			}
		}
		err := tx.QueryRow(ctx, "SELECT payload FROM project_records WHERE workspace_id=$1 AND project_id=$2 AND kind=$3 ORDER BY created_at DESC,id DESC LIMIT 1", workspace, projectID, kind).Scan(&data)
		if errors.Is(err, pgx.ErrNoRows) {
			data = json.RawMessage(`{"items":[]}`)
			return nil
		}
		return err
	})
	return data, err
}

func (r *Repository) Summary(ctx context.Context, workspace string) (map[string]any, error) {
	var queued, running, waiting, failed, completed, pending, gaps int64
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		err := tx.QueryRow(ctx, "SELECT count(*) FILTER(WHERE status='queued'),count(*) FILTER(WHERE status='running'),count(*) FILTER(WHERE status='waiting_review'),count(*) FILTER(WHERE status='failed'),count(*) FILTER(WHERE status='completed') FROM jobs").Scan(&queued, &running, &waiting, &failed, &completed)
		if err == nil {
			err = tx.QueryRow(ctx, "SELECT count(*) FROM command_outbox WHERE published_at IS NULL").Scan(&pending)
		}
		if err == nil {
			err = tx.QueryRow(ctx, "SELECT count(*) FROM runtime_event_inbox WHERE applied_at IS NULL").Scan(&gaps)
		}
		return err
	})
	return map[string]any{"jobs": map[string]int64{"queued": queued, "running": running, "waiting_review": waiting, "failed": failed, "completed": completed}, "outbox_pending": pending, "projection_pending": gaps}, err
}

func (r *Repository) Ready(ctx context.Context) error {
	if r.pool == nil {
		return errors.New("observability store unavailable")
	}
	return r.pool.Ping(ctx)
}

func (r *Repository) within(ctx context.Context, workspace string, fn func(pgx.Tx) error) error {
	if r.pool == nil {
		return errors.New("observability store unavailable")
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.pool, workspace)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	return fn(tx)
}

func projectExists(ctx context.Context, tx pgx.Tx, workspace, projectID string) error {
	var exists bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM projects WHERE id=$1 AND workspace_id=$2)", projectID, workspace).Scan(&exists); err != nil {
		return err
	}
	if !exists {
		return project.ErrNotFound
	}
	return nil
}
