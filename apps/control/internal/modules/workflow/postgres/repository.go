package postgres

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/workflow"
	"github.com/sakuya/ai-content-platform/apps/control/internal/platform/database"
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

func (r *Repository) writeAudit(ctx context.Context, tx pgx.Tx, principal identity.Principal, action, resourceType, resourceID string) error {
	if r.audit == nil {
		return errors.New("audit writer unavailable")
	}
	return r.audit(ctx, tx, principal, action, resourceType, resourceID)
}

const selectVersion = "SELECT id::text,workspace_id::text,name,version,status,definition,content_hash,created_at FROM workflow_templates "

func scan(row pgx.Row) (workflow.Version, error) {
	var v workflow.Version
	var data []byte
	err := row.Scan(&v.ID, &v.WorkspaceID, &v.Name, &v.Version, &v.Status, &data, &v.ContentHash, &v.CreatedAt)
	if err == nil {
		err = json.Unmarshal(data, &v.Definition)
	}
	if errors.Is(err, pgx.ErrNoRows) {
		err = identity.ErrNotFound
	}
	return v, err
}
func (r *Repository) within(ctx context.Context, workspace string, fn func(pgx.Tx) error) error {
	if r.pool == nil {
		return errors.New("workflow store unavailable")
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.pool, workspace)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
func (r *Repository) List(ctx context.Context, workspace string) ([]workflow.Version, error) {
	items := []workflow.Version{}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, selectVersion+"WHERE workspace_id=$1 OR workspace_id IS NULL ORDER BY name,version DESC", workspace)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			v, err := scan(rows)
			if err != nil {
				return err
			}
			items = append(items, v)
		}
		return rows.Err()
	})
	return items, err
}
func (r *Repository) Get(ctx context.Context, workspace, id string) (workflow.Version, error) {
	var v workflow.Version
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		var err error
		v, err = scan(tx.QueryRow(ctx, selectVersion+"WHERE id::text=$2 AND (workspace_id=$1 OR workspace_id IS NULL)", workspace, id))
		return err
	})
	return v, err
}
func (r *Repository) Create(ctx context.Context, p identity.Principal, name string, d workflow.Definition, hash string) (workflow.Version, error) {
	var id string
	err := r.within(ctx, p.WorkspaceID, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", p.WorkspaceID+":"+name); err != nil {
			return err
		}
		var version int
		if err := tx.QueryRow(ctx, "SELECT COALESCE(MAX(version),0)+1 FROM workflow_templates WHERE workspace_id=$1 AND name=$2", p.WorkspaceID, name).Scan(&version); err != nil {
			return err
		}
		data, err := json.Marshal(d)
		if err != nil {
			return err
		}
		if err = tx.QueryRow(ctx, "INSERT INTO workflow_templates(workspace_id,name,version,status,definition,content_hash) VALUES($1,$2,$3,'draft',$4,$5) RETURNING id::text", p.WorkspaceID, name, version, data, hash).Scan(&id); err != nil {
			return err
		}
		return r.writeAudit(ctx, tx, p, "workflow.created", "workflow", id)
	})
	if err != nil {
		return workflow.Version{}, err
	}
	return r.Get(ctx, p.WorkspaceID, id)
}
func (r *Repository) change(ctx context.Context, p identity.Principal, id, status string) (workflow.Version, error) {
	err := r.within(ctx, p.WorkspaceID, func(tx pgx.Tx) error {
		var owner *string
		var previous string
		if err := tx.QueryRow(ctx, "SELECT workspace_id::text,status FROM workflow_templates WHERE id::text=$1 FOR UPDATE", id).Scan(&owner, &previous); err != nil {
			return identity.ErrNotFound
		}
		if owner == nil || *owner != p.WorkspaceID {
			return identity.ErrForbidden
		}
		if status == "published" && previous == "disabled" {
			return identity.ErrConflict
		}
		if _, err := tx.Exec(ctx, "UPDATE workflow_templates SET status=$2 WHERE id::text=$1", id, status); err != nil {
			return err
		}
		return r.writeAudit(ctx, tx, p, "workflow."+status, "workflow", id)
	})
	if err != nil {
		return workflow.Version{}, err
	}
	return r.Get(ctx, p.WorkspaceID, id)
}
func (r *Repository) Publish(ctx context.Context, p identity.Principal, id string) (workflow.Version, error) {
	return r.change(ctx, p, id, "published")
}
func (r *Repository) Disable(ctx context.Context, p identity.Principal, id string) (workflow.Version, error) {
	return r.change(ctx, p, id, "disabled")
}
func (r *Repository) Frozen(ctx context.Context, workspace, template string) (workflow.Version, error) {
	var v workflow.Version
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		var err error
		v, err = scan(tx.QueryRow(ctx, selectVersion+"WHERE (workspace_id=$1 OR workspace_id IS NULL) AND status='published' AND definition->>'template'=$2 ORDER BY workspace_id NULLS LAST,version DESC,created_at DESC,id DESC LIMIT 1", workspace, template))
		return err
	})
	return v, err
}
