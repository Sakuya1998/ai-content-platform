package postgres

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

type ResourceVersion = governance.ResourceVersion

const resourceSelect = `SELECT r.id::text,v.id::text,r.workspace_id::text,r.kind,r.key,v.version,v.name,v.description,v.payload,v.status,v.content_hash,v.created_at FROM governance_resources r JOIN governance_resource_versions v ON v.resource_id=r.id `

func scanResource(row pgx.Row) (ResourceVersion, error) {
	var v ResourceVersion
	err := row.Scan(&v.ID, &v.VersionID, &v.WorkspaceID, &v.Kind, &v.Key, &v.Version, &v.Name, &v.Description, &v.Payload, &v.Status, &v.ContentHash, &v.CreatedAt)
	return v, err
}

func (g *GovernanceRepository) FrozenResource(ctx context.Context, workspace, id string) (ResourceVersion, error) {
	tx, err := workspaceTx(ctx, g.pool, workspace)
	if err != nil {
		return ResourceVersion{}, err
	}
	defer tx.Rollback(ctx)
	return scanResource(tx.QueryRow(ctx, resourceSelect+"WHERE r.workspace_id=$1 AND v.id::text=$2", workspace, id))
}

type resourceWrite struct {
	Key             string          `json:"key"`
	Name            string          `json:"name"`
	Description     *string         `json:"description"`
	Payload         json.RawMessage `json:"payload"`
	ExpectedVersion int             `json:"expected_version"`
}

func (g *GovernanceRepository) executeResource(w *catalogResult, r *catalogRequest, p Principal, kind string, parts []string) {
	ctx := r.Context()
	tx, err := workspaceTx(ctx, g.pool, p.WorkspaceID)
	if err != nil {
		catalogDBError(w, err)
		return
	}
	defer tx.Rollback(ctx)
	if len(parts) == 0 && r.action == governance.Read {
		query := resourceSelect + "WHERE r.workspace_id=$1 AND r.kind=$2 AND v.version=r.current_version"
		args := []any{p.WorkspaceID, kind}
		if status := r.filters["status"]; status != "" {
			query += " AND v.status=$3"
			args = append(args, status)
		}
		rows, e := tx.Query(ctx, query+" ORDER BY r.key", args...)
		if e != nil {
			catalogDBError(w, e)
			return
		}
		defer rows.Close()
		items := []ResourceVersion{}
		for rows.Next() {
			item, e := scanResource(rows)
			if e != nil {
				catalogDBError(w, e)
				return
			}
			items = append(items, item)
		}
		if e = rows.Err(); e != nil {
			catalogDBError(w, e)
			return
		}
		if kind == "styles" && (r.filters["status"] == "" || r.filters["status"] == "published") {
			for _, key := range []string{"jin_yong", "gu_long", "murakami", "yu_hua"} {
				v, _ := builtinGovernanceStyle(p.WorkspaceID, key)
				items = append(items, v)
			}
		}
		catalogComplete(w, 200, map[string]any{"items": items, "has_more": false})
		return
	}
	var current ResourceVersion
	id := ""
	action := ""
	statusCode := 200
	if len(parts) > 0 {
		id = parts[0]
		builtin := false
		if kind == "styles" && strings.HasPrefix(id, "system_style_") {
			current, builtin = builtinGovernanceStyle(p.WorkspaceID, id)
			if !builtin {
				catalogReject(w, 404, "style not found")
				return
			}
			if version := r.filters["version"]; version != "" && version != "1" {
				catalogReject(w, 404, "version not found")
				return
			}
			if r.action == governance.Read {
				if len(parts) == 2 && parts[1] == "versions" {
					catalogComplete(w, 200, map[string]any{"items": []ResourceVersion{current}})
				} else if len(parts) == 1 {
					catalogComplete(w, 200, current)
				} else {
					catalogNotFound(w)
				}
				return
			}
			if len(parts) != 2 || (parts[1] != "clone" && parts[1] != "copy") {
				catalogReject(w, 409, "built-in resources must be cloned before editing")
				return
			}
			action = parts[1]
		}
		if !builtin {
			// Serialize all edits and transitions of one resource.
			err = tx.QueryRow(ctx, "SELECT id::text FROM governance_resources WHERE workspace_id=$1 AND kind=$2 AND id::text=$3 FOR UPDATE", p.WorkspaceID, kind, id).Scan(&id)
			if err != nil {
				catalogDBError(w, err)
				return
			}
			if len(parts) == 2 {
				action = parts[1]
			}
			if len(parts) > 2 {
				catalogNotFound(w)
				return
			}
			if action == "versions" && r.action == governance.Read {
				rows, e := tx.Query(ctx, resourceSelect+"WHERE r.workspace_id=$1 AND r.kind=$2 AND r.id=$3 ORDER BY v.version DESC", p.WorkspaceID, kind, id)
				if e != nil {
					catalogDBError(w, e)
					return
				}
				defer rows.Close()
				items := []ResourceVersion{}
				for rows.Next() {
					v, e := scanResource(rows)
					if e != nil {
						catalogDBError(w, e)
						return
					}
					items = append(items, v)
				}
				if e = rows.Err(); e != nil {
					catalogDBError(w, e)
					return
				}
				catalogComplete(w, 200, map[string]any{"items": items})
				return
			}
			query := resourceSelect + "WHERE r.workspace_id=$1 AND r.kind=$2 AND r.id=$3 AND v.version=r.current_version"
			args := []any{p.WorkspaceID, kind, id}
			if version := r.filters["version"]; version != "" && r.action == governance.Read {
				query = resourceSelect + "WHERE r.workspace_id=$1 AND r.kind=$2 AND r.id=$3 AND v.version::text=$4"
				args = append(args, version)
			}
			current, err = scanResource(tx.QueryRow(ctx, query, args...))
			if err != nil {
				catalogDBError(w, err)
				return
			}
			if r.action == governance.Read && action == "" {
				catalogComplete(w, 200, current)
				return
			}
		}
	}
	var input resourceWrite
	if !decodeCatalogJSON(w, r, &input) {
		return
	}
	if input.ExpectedVersion > 0 && input.ExpectedVersion != current.Version {
		catalogReject(w, 409, "resource version changed")
		return
	}
	next := current
	next.Version++
	next.Status = "draft"
	next.VersionID = uuid.NewString()
	if len(parts) == 0 && r.action == governance.Create || action == "clone" || action == "copy" {
		if r.action != governance.Create {
			catalogUnsupported(w)
			return
		}
		if !regexpResourceKey(input.Key) {
			catalogReject(w, 422, "invalid resource key")
			return
		}
		if kind == "styles" {
			if _, reserved := governanceBuiltinStyles[input.Key]; reserved {
				catalogReject(w, 409, "built-in style key is reserved")
				return
			}
		}
		if strings.TrimSpace(input.Name) == "" {
			catalogReject(w, 422, "name required")
			return
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
	} else if action == "publish" || action == "disable" {
		if r.action != governance.Create {
			catalogUnsupported(w)
			return
		}
		if action == "publish" {
			next.Status = "published"
		} else {
			next.Status = "disabled"
		}
	} else if action == "" && (r.action == governance.Update || r.action == governance.Replace) {
		if input.Key != "" && input.Key != current.Key {
			catalogReject(w, 422, "resource key is immutable; clone to change it")
			return
		}
		if input.Name != "" {
			next.Name = input.Name
		}
	} else {
		catalogUnsupported(w)
		return
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
		if normalized, e := normalizeGovernancePayload(kind, next.Payload); e != nil {
			catalogReject(w, 422, e.Error())
			return
		} else {
			next.Payload = normalized
		}
	}
	if kind == "content_types" {
		var payload map[string]any
		_ = json.Unmarshal(next.Payload, &payload)
		if parent, ok := payload["parent_id"].(string); ok && parent != "" {
			var exists bool
			err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM governance_resources WHERE workspace_id=$1 AND kind='content_types' AND id::text=$2 AND id<>$3)", p.WorkspaceID, parent, id).Scan(&exists)
			if err != nil {
				catalogDBError(w, err)
				return
			}
			if !exists {
				catalogReject(w, 422, "parent content type not found")
				return
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
		err = g.writeAudit(ctx, tx, p, "governance.resource."+next.Status, kind, id)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		catalogDBError(w, err)
		return
	}
	catalogComplete(w, statusCode, next)
}
