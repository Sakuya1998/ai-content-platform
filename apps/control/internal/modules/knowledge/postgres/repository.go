package postgres

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/project"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/database"
)

type Repository struct{ pool *pgxpool.Pool }

func NewRepository(pool *pgxpool.Pool) *Repository { return &Repository{pool: pool} }

func (r *Repository) List(ctx context.Context, workspace, projectID, kind string) ([]json.RawMessage, error) {
	items := []json.RawMessage{}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		if projectID != "" {
			if err := projectExists(ctx, tx, workspace, projectID); err != nil {
				return err
			}
		}
		rows, err := tx.Query(ctx, "SELECT payload FROM project_records WHERE workspace_id=$1 AND ($2='' OR project_id::text=$2) AND kind=$3 AND NOT stale ORDER BY created_at DESC,id LIMIT 100", workspace, projectID, kind)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var item json.RawMessage
			if err = rows.Scan(&item); err != nil {
				return err
			}
			items = append(items, item)
		}
		return rows.Err()
	})
	return items, err
}

func (r *Repository) within(ctx context.Context, workspace string, fn func(pgx.Tx) error) error {
	if r.pool == nil {
		return errors.New("knowledge store unavailable")
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
