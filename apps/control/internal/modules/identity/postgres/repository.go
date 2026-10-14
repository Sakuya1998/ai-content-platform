package postgres

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/sakuya/ai-content-platform/apps/control/internal/platform/database"
)

type IdentityRepository struct{ pool *pgxpool.Pool }

func NewRepository(pool *pgxpool.Pool) *IdentityRepository {
	return &IdentityRepository{pool: pool}
}

func RecordAudit(ctx context.Context, tx pgx.Tx, workspaceID, actorID, action, resourceType, resourceID string) error {
	var actor any
	if actorID != "" {
		actor = actorID
	}
	_, err := tx.Exec(ctx, "INSERT INTO audit_logs(workspace_id,actor_id,action,resource_type,resource_id) VALUES($1,$2,$3,$4,$5)", workspaceID, actor, action, resourceType, resourceID)
	return err
}

func RecordProjectAudit(ctx context.Context, tx pgx.Tx, workspaceID, action, projectID string) error {
	principal, _ := identity.FromContext(ctx)
	return RecordAudit(ctx, tx, workspaceID, principal.UserID, action, "project", projectID)
}

func RecordPrincipalAudit(ctx context.Context, tx pgx.Tx, principal identity.Principal, action, resourceType, resourceID string) error {
	return RecordAudit(ctx, tx, principal.WorkspaceID, principal.UserID, action, resourceType, resourceID)
}

func identityError(err error) error {
	var e *pgconn.PgError
	if errors.Is(err, pgx.ErrNoRows) {
		return identity.ErrNotFound
	}
	if errors.As(err, &e) && (e.Code == "23505" || e.Code == "23503") {
		return identity.ErrConflict
	}
	return err
}
func (r *IdentityRepository) within(ctx context.Context, p identity.Principal, fn func(pgx.Tx) error) error {
	if r.pool == nil {
		return errors.New("identity storage unavailable")
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.pool, p.WorkspaceID)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if err = fn(tx); err != nil {
		return identityError(err)
	}
	return identityError(tx.Commit(ctx))
}
func auditIdentity(ctx context.Context, tx pgx.Tx, p identity.Principal, action, kind, id string) error {
	return RecordAudit(ctx, tx, p.WorkspaceID, p.UserID, action, kind, id)
}
func (r *IdentityRepository) Session(ctx context.Context, hash string) (identity.Principal, error) {
	var p identity.Principal
	if r.pool == nil {
		return p, identity.ErrUnauthorized
	}
	err := r.pool.QueryRow(ctx, `SELECT u.id::text,u.workspace_id::text,coalesce(u.username,''),u.email,u.display_name,u.role FROM identity_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=$1 AND s.expires_at>now() AND NOT u.disabled`, hash).Scan(&p.UserID, &p.WorkspaceID, &p.Username, &p.Email, &p.DisplayName, &p.Role)
	return p, identityError(err)
}
func (r *IdentityRepository) FindLogin(ctx context.Context, identifier string) (identity.Principal, string, error) {
	var p identity.Principal
	var hash string
	if r.pool == nil {
		return p, "", identity.ErrUnauthorized
	}
	err := r.pool.QueryRow(ctx, "SELECT id::text,workspace_id::text,coalesce(username,''),email,display_name,role,password_hash FROM users WHERE (lower(username)=lower($1) OR lower(email)=lower($1)) AND password_hash IS NOT NULL AND NOT disabled", identifier).Scan(&p.UserID, &p.WorkspaceID, &p.Username, &p.Email, &p.DisplayName, &p.Role, &hash)
	return p, hash, identityError(err)
}
func saveSession(ctx context.Context, tx pgx.Tx, p identity.Principal, hash string, expires time.Time, action string) error {
	if _, err := tx.Exec(ctx, "INSERT INTO identity_sessions(token_hash,user_id,expires_at) VALUES($1,$2,$3)", hash, p.UserID, expires); err != nil {
		return err
	}
	return auditIdentity(ctx, tx, p, action, "user", p.UserID)
}
func (r *IdentityRepository) Register(ctx context.Context, p identity.Principal, password, name, session string, expires time.Time) error {
	return r.within(ctx, p, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,$2)", p.WorkspaceID, name); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, "INSERT INTO users(id,workspace_id,username,email,display_name,password_hash,role) VALUES($1,$2,$3,$4,$5,$6,'owner')", p.UserID, p.WorkspaceID, p.Username, p.Email, p.DisplayName, password); err != nil {
			return err
		}
		return saveSession(ctx, tx, p, session, expires, "auth.register")
	})
}
func (r *IdentityRepository) CreateSession(ctx context.Context, p identity.Principal, hash string, expires time.Time) error {
	return r.within(ctx, p, func(tx pgx.Tx) error { return saveSession(ctx, tx, p, hash, expires, "auth.login") })
}
func (r *IdentityRepository) Logout(ctx context.Context, p identity.Principal, hash string) error {
	return r.within(ctx, p, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "DELETE FROM identity_sessions WHERE token_hash=$1 AND user_id=$2", hash, p.UserID); err != nil {
			return err
		}
		return auditIdentity(ctx, tx, p, "auth.logout", "user", p.UserID)
	})
}
func (r *IdentityRepository) Workspace(ctx context.Context, p identity.Principal) (identity.Workspace, error) {
	var result identity.Workspace
	err := r.within(ctx, p, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT id::text,name FROM workspaces WHERE id=$1", p.WorkspaceID).Scan(&result.ID, &result.Name)
	})
	return result, err
}
func (r *IdentityRepository) UpdateWorkspace(ctx context.Context, p identity.Principal, name string) (identity.Workspace, error) {
	var result identity.Workspace
	err := r.within(ctx, p, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "UPDATE workspaces SET name=$2 WHERE id=$1", p.WorkspaceID, name); err != nil {
			return err
		}
		if err := auditIdentity(ctx, tx, p, "workspace.updated", "workspace", p.WorkspaceID); err != nil {
			return err
		}
		return tx.QueryRow(ctx, "SELECT id::text,name FROM workspaces WHERE id=$1", p.WorkspaceID).Scan(&result.ID, &result.Name)
	})
	return result, err
}
func (r *IdentityRepository) AuditLogs(ctx context.Context, p identity.Principal) ([]identity.AuditLog, error) {
	items := []identity.AuditLog{}
	err := r.within(ctx, p, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, "SELECT id,action,resource_type,coalesce(resource_id::text,''),created_at FROM audit_logs WHERE workspace_id=$1 ORDER BY id DESC LIMIT 200", p.WorkspaceID)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var item identity.AuditLog
			if err = rows.Scan(&item.ID, &item.Action, &item.ResourceType, &item.ResourceID, &item.CreatedAt); err != nil {
				return err
			}
			items = append(items, item)
		}
		return rows.Err()
	})
	return items, err
}
func (r *IdentityRepository) Members(ctx context.Context, p identity.Principal) ([]identity.Principal, error) {
	items := []identity.Principal{}
	err := r.within(ctx, p, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, "SELECT id::text,workspace_id::text,coalesce(username,''),email,display_name,role FROM users WHERE workspace_id=$1 AND NOT disabled ORDER BY created_at", p.WorkspaceID)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var m identity.Principal
			if err = rows.Scan(&m.UserID, &m.WorkspaceID, &m.Username, &m.Email, &m.DisplayName, &m.Role); err != nil {
				return err
			}
			items = append(items, m)
		}
		return rows.Err()
	})
	return items, err
}
func lockIdentityOwner(ctx context.Context, tx pgx.Tx, p identity.Principal) error {
	if _, err := tx.Exec(ctx, "SELECT id FROM workspaces WHERE id=$1 FOR UPDATE", p.WorkspaceID); err != nil {
		return err
	}
	var role string
	if err := tx.QueryRow(ctx, "SELECT role FROM users WHERE id=$1 AND workspace_id=$2 AND NOT disabled", p.UserID, p.WorkspaceID).Scan(&role); err != nil {
		return err
	}
	if role != "owner" {
		return identity.ErrForbidden
	}
	return nil
}
func (r *IdentityRepository) CreateMember(ctx context.Context, p, m identity.Principal, hash string) error {
	return r.within(ctx, p, func(tx pgx.Tx) error {
		if err := lockIdentityOwner(ctx, tx, p); err != nil {
			return err
		}
		if _, err := tx.Exec(ctx, "INSERT INTO users(id,workspace_id,username,email,display_name,password_hash,role) VALUES($1,$2,$3,$4,$5,$6,$7)", m.UserID, p.WorkspaceID, m.Username, m.Email, m.DisplayName, hash, m.Role); err != nil {
			return err
		}
		return auditIdentity(ctx, tx, p, "workspace.member_created", "user", m.UserID)
	})
}
func (r *IdentityRepository) ChangeMember(ctx context.Context, p identity.Principal, id, role string, remove bool) error {
	return r.within(ctx, p, func(tx pgx.Tx) error {
		if err := lockIdentityOwner(ctx, tx, p); err != nil {
			return err
		}
		var current string
		if err := tx.QueryRow(ctx, "SELECT role FROM users WHERE id::text=$1 AND workspace_id=$2 AND NOT disabled", id, p.WorkspaceID).Scan(&current); err != nil {
			return err
		}
		if current == "owner" && (remove || role != "owner") {
			var count int
			if err := tx.QueryRow(ctx, "SELECT count(*) FROM users WHERE workspace_id=$1 AND role='owner' AND NOT disabled", p.WorkspaceID).Scan(&count); err != nil {
				return err
			}
			if count <= 1 {
				return identity.ErrRetainOwner
			}
		}
		action := "workspace.member_role_updated"
		if remove {
			if _, err := tx.Exec(ctx, "UPDATE users SET disabled=true WHERE id=$1 AND workspace_id=$2", id, p.WorkspaceID); err != nil {
				return err
			}
			action = "workspace.member_removed"
		} else {
			if _, err := tx.Exec(ctx, "UPDATE users SET role=$3 WHERE id=$1 AND workspace_id=$2", id, p.WorkspaceID, role); err != nil {
				return err
			}
		}
		if _, err := tx.Exec(ctx, "DELETE FROM identity_sessions WHERE user_id=$1", id); err != nil {
			return err
		}
		return auditIdentity(ctx, tx, p, action, "user", id)
	})
}
