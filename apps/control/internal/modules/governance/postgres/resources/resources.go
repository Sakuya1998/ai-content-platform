package resources

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
)

// ResourceVersion is an alias to the domain type
type ResourceVersion = governance.ResourceVersion

const resourceSelect = `SELECT r.id::text,v.id::text,r.workspace_id::text,r.kind,r.key,v.version,v.name,v.description,v.payload,v.status,v.content_hash,v.created_at FROM governance_resources r JOIN governance_resource_versions v ON v.resource_id=r.id `

// ScanResource reads a ResourceVersion from a database row
func ScanResource(row pgx.Row) (ResourceVersion, error) {
	var v ResourceVersion
	err := row.Scan(&v.ID, &v.VersionID, &v.WorkspaceID, &v.Kind, &v.Key, &v.Version, &v.Name, &v.Description, &v.Payload, &v.Status, &v.ContentHash, &v.CreatedAt)
	return v, err
}

type resourceWrite struct {
	Key             string          `json:"key"`
	Name            string          `json:"name"`
	Description     *string         `json:"description"`
	Payload         json.RawMessage `json:"payload"`
	ExpectedVersion int             `json:"expected_version"`
}

// Repository provides resource CRUD operations
type Repository struct {
	pool interface {
		Begin(context.Context) (pgx.Tx, error)
	}
	writeAudit      func(context.Context, pgx.Tx, Principal, string, string, string) error
	normalizePayload func(string, json.RawMessage) (json.RawMessage, error)
	builtinStyle    func(workspace, key string) (ResourceVersion, bool)
	regexpKey       func(string) bool
	builtinStyleMap map[string]bool
}

// Principal represents the authenticated user context
type Principal struct {
	WorkspaceID string
	UserID      string
}

// NewRepository creates a new resource repository
func NewRepository(
	pool interface {
		Begin(context.Context) (pgx.Tx, error)
	},
	writeAudit func(context.Context, pgx.Tx, Principal, string, string, string) error,
	normalizePayload func(string, json.RawMessage) (json.RawMessage, error),
	builtinStyle func(workspace, key string) (ResourceVersion, bool),
	regexpKey func(string) bool,
	builtinStyleMap map[string]bool,
) *Repository {
	return &Repository{
		pool:            pool,
		writeAudit:      writeAudit,
		normalizePayload: normalizePayload,
		builtinStyle:    builtinStyle,
		regexpKey:       regexpKey,
		builtinStyleMap: builtinStyleMap,
	}
}

// FrozenResource retrieves a specific frozen resource version by ID
func (repo *Repository) FrozenResource(ctx context.Context, pool interface{ Begin(context.Context) (pgx.Tx, error) }, workspace, id string) (ResourceVersion, error) {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return ResourceVersion{}, err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, "SET LOCAL rls.workspace_id = $1", workspace)
	if err != nil {
		return ResourceVersion{}, err
	}
	return ScanResource(tx.QueryRow(ctx, resourceSelect+"WHERE r.workspace_id=$1 AND v.id::text=$2", workspace, id))
}

// ExecuteResourceCatalogOp handles CRUD operations for resources via catalog protocol
func (repo *Repository) ExecuteResourceCatalogOp(
	ctx context.Context,
	p Principal,
	kind string,
	parts []string,
	action governance.Action,
	filters map[string]string,
	body json.RawMessage,
) (statusCode int, result any, err error) {
	tx, err := repo.pool.Begin(ctx)
	if err != nil {
		return 0, nil, err
	}
	defer tx.Rollback(ctx)

	_, err = tx.Exec(ctx, "SET LOCAL rls.workspace_id = $1", p.WorkspaceID)
	if err != nil {
		return 0, nil, err
	}

	// List resources
	if len(parts) == 0 && action == governance.Read {
		query := resourceSelect + "WHERE r.workspace_id=$1 AND r.kind=$2 AND v.version=r.current_version"
		args := []any{p.WorkspaceID, kind}
		if status := filters["status"]; status != "" {
			query += " AND v.status=$3"
			args = append(args, status)
		}
		rows, e := tx.Query(ctx, query+" ORDER BY r.key", args...)
		if e != nil {
			return 0, nil, e
		}
		defer rows.Close()
		items := []ResourceVersion{}
		for rows.Next() {
			item, e := ScanResource(rows)
			if e != nil {
				return 0, nil, e
			}
			items = append(items, item)
		}
		if e = rows.Err(); e != nil {
			return 0, nil, e
		}
		// Add builtin styles
		if kind == "styles" && (filters["status"] == "" || filters["status"] == "published") {
			for _, key := range []string{"jin_yong", "gu_long", "murakami", "yu_hua"} {
				v, _ := repo.builtinStyle(p.WorkspaceID, key)
				items = append(items, v)
			}
		}
		return 200, map[string]any{"items": items, "has_more": false}, tx.Commit(ctx)
	}

	var current ResourceVersion
	id := ""
	opAction := ""
	statusCode = 200

	if len(parts) > 0 {
		id = parts[0]
		builtin := false

		// Handle builtin styles
		if kind == "styles" && strings.HasPrefix(id, "system_style_") {
			current, builtin = repo.builtinStyle(p.WorkspaceID, id)
			if !builtin {
				return 404, nil, governance.ErrNotFound
			}
			if version := filters["version"]; version != "" && version != "1" {
				return 404, nil, governance.ErrNotFound
			}
			if action == governance.Read {
				if len(parts) == 2 && parts[1] == "versions" {
					return 200, map[string]any{"items": []ResourceVersion{current}}, tx.Commit(ctx)
				} else if len(parts) == 1 {
					return 200, current, tx.Commit(ctx)
				} else {
					return 404, nil, governance.ErrNotFound
				}
			}
			if len(parts) != 2 || (parts[1] != "clone" && parts[1] != "copy") {
				return 409, nil, governance.ErrConflict
			}
			opAction = parts[1]
		}

		if !builtin {
			// Serialize all edits and transitions of one resource
			err = tx.QueryRow(ctx, "SELECT id::text FROM governance_resources WHERE workspace_id=$1 AND kind=$2 AND id::text=$3 FOR UPDATE", p.WorkspaceID, kind, id).Scan(&id)
			if err != nil {
				return 0, nil, err
			}
			if len(parts) == 2 {
				opAction = parts[1]
			}
			if len(parts) > 2 {
				return 404, nil, governance.ErrNotFound
			}
			if opAction == "versions" && action == governance.Read {
				rows, e := tx.Query(ctx, resourceSelect+"WHERE r.workspace_id=$1 AND r.kind=$2 AND r.id=$3 ORDER BY v.version DESC", p.WorkspaceID, kind, id)
				if e != nil {
					return 0, nil, e
				}
				defer rows.Close()
				items := []ResourceVersion{}
				for rows.Next() {
					v, e := ScanResource(rows)
					if e != nil {
						return 0, nil, e
					}
					items = append(items, v)
				}
				if e = rows.Err(); e != nil {
					return 0, nil, e
				}
				return 200, map[string]any{"items": items}, tx.Commit(ctx)
			}
			query := resourceSelect + "WHERE r.workspace_id=$1 AND r.kind=$2 AND r.id=$3 AND v.version=r.current_version"
			args := []any{p.WorkspaceID, kind, id}
			if version := filters["version"]; version != "" && action == governance.Read {
				query = resourceSelect + "WHERE r.workspace_id=$1 AND r.kind=$2 AND r.id=$3 AND v.version::text=$4"
				args = append(args, version)
			}
			current, err = ScanResource(tx.QueryRow(ctx, query, args...))
			if err != nil {
				return 0, nil, err
			}
			if action == governance.Read && opAction == "" {
				return 200, current, tx.Commit(ctx)
			}
		}
	}

	var input resourceWrite
	if len(body) > 0 {
		if err := json.Unmarshal(body, &input); err != nil {
			return 422, nil, err
		}
	}

	if input.ExpectedVersion > 0 && input.ExpectedVersion != current.Version {
		return 409, nil, governance.ErrConflict
	}

	next := current
	next.Version++
	next.Status = "draft"
	next.VersionID = uuid.NewString()

	if len(parts) == 0 && action == governance.Create || opAction == "clone" || opAction == "copy" {
		if action != governance.Create {
			return 405, nil, governance.ErrMethodNotAllowed
		}
		if !repo.regexpKey(input.Key) {
			return 422, nil, governance.ErrInvalidKey
		}
		if kind == "styles" {
			if repo.builtinStyleMap[input.Key] {
				return 409, nil, governance.ErrConflict
			}
		}
		if strings.TrimSpace(input.Name) == "" {
			return 422, nil, governance.ErrNameRequired
		}
		id = uuid.NewString()
		next.ID = id
		next.Version = 1
		next.Key = input.Key
		next.Name = input.Name
		next.WorkspaceID = p.WorkspaceID
		next.Kind = kind
		_, err = tx.Exec(ctx, "INSERT INTO governance_resources(id,workspace_id,kind,key) VALUES($1,$2,$3,$4)", id, p.WorkspaceID, kind, input.Key)
		statusCode = 201
	} else if opAction == "publish" || opAction == "disable" {
		if action != governance.Create {
			return 405, nil, governance.ErrMethodNotAllowed
		}
		if opAction == "publish" {
			next.Status = "published"
		} else {
			next.Status = "disabled"
		}
	} else if opAction == "" && (action == governance.Update || action == governance.Replace) {
		if input.Key != "" && input.Key != current.Key {
			return 422, nil, governance.ErrKeyImmutable
		}
		if input.Name != "" {
			next.Name = input.Name
		}
	} else {
		return 405, nil, governance.ErrMethodNotAllowed
	}

	if input.Description != nil {
		next.Description = *input.Description
	}
	if len(input.Payload) > 0 {
		next.Payload = input.Payload
	}
	if len(next.Payload) == 0 {
		next.Payload = json.RawMessage(`{}`)
	}
	if len(input.Payload) > 0 || current.ID == "" {
		if normalized, e := repo.normalizePayload(kind, next.Payload); e != nil {
			return 422, nil, e
		} else {
			next.Payload = normalized
		}
	}

	// Validate content type parent
	if kind == "content_types" {
		var payload map[string]any
		_ = json.Unmarshal(next.Payload, &payload)
		if parent, ok := payload["parent_id"].(string); ok && parent != "" {
			var exists bool
			err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM governance_resources WHERE workspace_id=$1 AND kind='content_types' AND id::text=$2 AND id<>$3)", p.WorkspaceID, parent, id).Scan(&exists)
			if err != nil {
				return 0, nil, err
			}
			if !exists {
				return 422, nil, governance.ErrParentNotFound
			}
		}
	}

	digest := sha256.Sum256(next.Payload)
	next.ContentHash = hex.EncodeToString(digest[:])
	next.CreatedAt = time.Now().UTC()

	if err == nil {
		_, err = tx.Exec(ctx, "INSERT INTO governance_resource_versions(id,workspace_id,resource_id,version,name,description,payload,status,content_hash,created_by) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", next.VersionID, p.WorkspaceID, id, next.Version, next.Name, next.Description, next.Payload, next.Status, next.ContentHash, p.UserID)
	}
	if err == nil {
		_, err = tx.Exec(ctx, "UPDATE governance_resources SET current_version=$3 WHERE workspace_id=$1 AND id=$2", p.WorkspaceID, id, next.Version)
	}
	if err == nil {
		err = repo.writeAudit(ctx, tx, p, "governance.resource."+next.Status, kind, id)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}

	return statusCode, next, err
}
