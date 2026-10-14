package postgres

import (
	"encoding/json"
	"errors"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/governance"
)

type creationPolicyWrite struct {
	Key             string          `json:"key"`
	Name            string          `json:"name"`
	Payload         json.RawMessage `json:"payload"`
	ExpectedVersion int             `json:"expected_version"`
	VersionID       string          `json:"version_id"`
	Workflow        string          `json:"workflow"`
}

func (a *PolicyRepository) executePolicy(w *catalogResult, r *catalogRequest, p Principal, tx pgx.Tx, parts []string) {
	ctx := r.Context()
	if len(parts) > 3 {
		catalogNotFound(w)
		return
	}
	if len(parts) == 0 && r.action == governance.Read {
		rows, err := tx.Query(ctx, creationPolicySelect+`WHERE (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version ORDER BY p.key,p.workspace_id NULLS LAST`, p.WorkspaceID)
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		defer rows.Close()
		items := []CreationPolicyVersion{}
		for rows.Next() {
			v, e := scanCreationPolicy(rows)
			if e != nil {
				catalogPromptError(w, e)
				return
			}
			if status := r.filters["status"]; status == "" || status == v.Status {
				items = append(items, v)
			}
		}
		if err = rows.Err(); err != nil {
			catalogPromptError(w, err)
			return
		}
		catalogComplete(w, 200, map[string]any{"items": items, "has_more": false})
		return
	}
	var current CreationPolicyVersion
	var err error
	action := ""
	if len(parts) > 0 {
		if _, err = uuid.Parse(parts[0]); err != nil {
			catalogReject(w, 404, "creation policy not found")
			return
		}
		current, err = scanCreationPolicy(tx.QueryRow(ctx, creationPolicySelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version`, p.WorkspaceID, parts[0]))
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		if len(parts) > 1 {
			action = parts[1]
		}
		if r.action == governance.Read {
			if action == "versions" && len(parts) == 2 {
				rows, e := tx.Query(ctx, creationPolicySelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) ORDER BY v.version DESC`, p.WorkspaceID, current.ID)
				if e != nil {
					catalogPromptError(w, e)
					return
				}
				defer rows.Close()
				items := []CreationPolicyVersion{}
				for rows.Next() {
					v, e := scanCreationPolicy(rows)
					if e != nil {
						catalogPromptError(w, e)
						return
					}
					items = append(items, v)
				}
				if e = rows.Err(); e != nil {
					catalogPromptError(w, e)
					return
				}
				catalogComplete(w, 200, map[string]any{"items": items})
				return
			}
			if action == "versions" && len(parts) == 3 {
				current, err = scanCreationPolicy(tx.QueryRow(ctx, creationPolicySelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND (v.id::text=$3 OR v.version::text=$3)`, p.WorkspaceID, current.ID, parts[2]))
				if err != nil {
					catalogPromptError(w, err)
					return
				}
			} else if action != "" {
				catalogNotFound(w)
				return
			}
			catalogComplete(w, 200, current)
			return
		}
	}
	clone := action == "clone" && r.action == governance.Create
	creating := len(parts) == 0 || clone
	if len(parts) > 0 && current.WorkspaceID == nil && !clone {
		catalogReject(w, 409, "platform creation policies must be cloned before editing")
		return
	}
	if !(len(parts) == 0 && r.action == governance.Create || len(parts) == 1 && r.action == governance.Update || len(parts) == 2 && r.action == governance.Create && (action == "clone" || action == "publish" || action == "disable" || action == "versions")) {
		catalogUnsupported(w)
		return
	}
	var in creationPolicyWrite
	if err = decodeCatalogBody(r, &in); err != nil {
		catalogPromptError(w, err)
		return
	}
	if !creating {
		var locked int
		if err = tx.QueryRow(ctx, `SELECT current_version FROM creation_policy_sets WHERE id=$2 AND workspace_id=$1 FOR UPDATE`, p.WorkspaceID, current.ID).Scan(&locked); err != nil {
			catalogPromptError(w, err)
			return
		}
		if locked != current.Version {
			current, err = scanCreationPolicy(tx.QueryRow(ctx, creationPolicySelect+`WHERE p.id=$2 AND p.workspace_id=$1 AND v.version=p.current_version`, p.WorkspaceID, current.ID))
			if err != nil {
				catalogPromptError(w, err)
				return
			}
		}
		if in.ExpectedVersion != 0 && in.ExpectedVersion != current.Version {
			catalogReject(w, 409, "creation policy version conflict")
			return
		}
	}
	next := current
	if in.VersionID != "" {
		if len(parts) == 0 {
			catalogReject(w, 422, "source version requires policy identity")
			return
		}
		next, err = scanCreationPolicy(tx.QueryRow(ctx, creationPolicySelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.id::text=$3`, p.WorkspaceID, current.ID, in.VersionID))
		if err != nil {
			catalogPromptError(w, err)
			return
		}
	}
	if in.Key != "" {
		next.Key = in.Key
	}
	if in.Name != "" {
		next.Name = in.Name
	}
	if len(in.Payload) > 0 {
		next.Payload = in.Payload
	}
	if creating {
		next.ID = uuid.NewString()
		next.WorkspaceID = &p.WorkspaceID
		next.Version = 1
		if clone && in.Key == "" {
			catalogReject(w, 422, "clone key required")
			return
		}
	} else {
		next.ID = current.ID
		next.Version = current.Version + 1
		next.WorkspaceID = current.WorkspaceID
	}
	if strings.TrimSpace(next.Key) == "" || strings.TrimSpace(next.Name) == "" {
		catalogReject(w, 422, "key and name required")
		return
	}
	next.Status = "draft"
	if action == "publish" {
		next.Status = "published"
	}
	if action == "disable" {
		next.Status = "disabled"
	}
	workflow := ""
	if next.Status == "published" {
		workflow = in.Workflow
	}
	if err = validateCreationPolicy(next.Payload, workflow); err != nil {
		catalogPromptError(w, err)
		return
	}
	next.ContentHash, err = creationPolicyHash(next.Payload)
	if err != nil {
		catalogPromptError(w, err)
		return
	}
	if creating {
		_, err = tx.Exec(ctx, `INSERT INTO creation_policy_sets(id,workspace_id,key,name,current_version) VALUES($1,$2,$3,$4,$5)`, next.ID, p.WorkspaceID, next.Key, next.Name, next.Version)
	} else {
		_, err = tx.Exec(ctx, `UPDATE creation_policy_sets SET key=$3,name=$4,current_version=$5 WHERE id=$2 AND workspace_id=$1`, p.WorkspaceID, next.ID, next.Key, next.Name, next.Version)
	}
	if err != nil {
		catalogPromptError(w, err)
		return
	}
	next.VersionID = uuid.NewString()
	err = tx.QueryRow(ctx, `INSERT INTO creation_policy_versions(id,policy_set_id,workspace_id,version,status,payload,content_hash,created_by) VALUES($1,$2,$3,$4,$5,$6,$7,$8) RETURNING created_at`, next.VersionID, next.ID, p.WorkspaceID, next.Version, next.Status, next.Payload, next.ContentHash, p.UserID).Scan(&next.CreatedAt)
	if err == nil {
		err = a.writeAudit(ctx, tx, p, "creation_policy."+next.Status, "creation_policy", next.ID)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		catalogPromptError(w, err)
		return
	}
	code := 200
	if creating {
		code = 201
	}
	catalogComplete(w, code, next)
}

type creationPolicyBinding struct {
	ID              string  `json:"id"`
	WorkspaceID     *string `json:"workspace_id"`
	Workflow        string  `json:"workflow"`
	PolicyVersionID string  `json:"policy_version_id"`
	CreatedAt       any     `json:"created_at"`
}

func (a *PolicyRepository) executeBinding(w *catalogResult, r *catalogRequest, p Principal, tx pgx.Tx, parts []string) {
	ctx := r.Context()
	if len(parts) != 1 && !(len(parts) == 2 && parts[1] == "history" && r.action == governance.Read) {
		catalogNotFound(w)
		return
	}
	workflow := parts[0]
	if _, err := workflowPromptNodes(workflow); err != nil {
		catalogPromptError(w, err)
		return
	}
	if r.action == governance.Read {
		table := "workflow_creation_policy_bindings"
		if len(parts) == 2 {
			table = "workflow_creation_policy_binding_history"
		}
		rows, err := tx.Query(ctx, `SELECT id::text,workspace_id::text,workflow,policy_version_id::text,created_at FROM `+table+` WHERE workflow=$2 AND (workspace_id=$1 OR workspace_id IS NULL) ORDER BY workspace_id NULLS LAST,created_at DESC`, p.WorkspaceID, workflow)
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		defer rows.Close()
		items := []creationPolicyBinding{}
		for rows.Next() {
			var item creationPolicyBinding
			if err = rows.Scan(&item.ID, &item.WorkspaceID, &item.Workflow, &item.PolicyVersionID, &item.CreatedAt); err != nil {
				catalogPromptError(w, err)
				return
			}
			items = append(items, item)
		}
		if err = rows.Err(); err != nil {
			catalogPromptError(w, err)
			return
		}
		catalogComplete(w, 200, map[string]any{"items": items})
		return
	}
	if r.action != governance.Replace && r.action != governance.Delete {
		catalogUnsupported(w)
		return
	}
	// Serialize override creation, replacement and removal for this workspace.
	if _, err := tx.Exec(ctx, `SELECT id FROM workspaces WHERE id=$1 FOR UPDATE`, p.WorkspaceID); err != nil {
		catalogPromptError(w, err)
		return
	}
	if r.action == governance.Delete {
		var id string
		err := tx.QueryRow(ctx, `DELETE FROM workflow_creation_policy_bindings WHERE workspace_id=$1 AND workflow=$2 RETURNING id::text`, p.WorkspaceID, workflow).Scan(&id)
		if err == nil {
			err = a.writeAudit(ctx, tx, p, "creation_policy_binding.deleted", "creation_policy_binding", id)
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		catalogComplete(w, 204, nil)
		return
	}
	var in struct {
		PolicyVersionID         string `json:"policy_version_id"`
		ExpectedPolicyVersionID string `json:"expected_policy_version_id"`
	}
	if err := decodeCatalogBody(r, &in); err != nil {
		catalogPromptError(w, err)
		return
	}
	v, err := creationPolicyForWorkflow(ctx, tx, p.WorkspaceID, in.PolicyVersionID, workflow)
	if err != nil {
		catalogPromptError(w, err)
		return
	}
	if in.ExpectedPolicyVersionID != "" {
		var current string
		err = tx.QueryRow(ctx, `SELECT policy_version_id::text FROM workflow_creation_policy_bindings WHERE workflow=$2 AND (workspace_id=$1 OR workspace_id IS NULL) ORDER BY workspace_id NULLS LAST LIMIT 1`, p.WorkspaceID, workflow).Scan(&current)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			catalogPromptError(w, err)
			return
		}
		if current != in.ExpectedPolicyVersionID {
			catalogReject(w, 409, "creation policy binding conflict")
			return
		}
	}
	var id string
	err = tx.QueryRow(ctx, `INSERT INTO workflow_creation_policy_bindings(workspace_id,workflow,policy_version_id,created_by) VALUES($1,$2,$3,$4) ON CONFLICT(workspace_id,workflow) DO UPDATE SET policy_version_id=EXCLUDED.policy_version_id,created_by=EXCLUDED.created_by,created_at=now() RETURNING id::text`, p.WorkspaceID, workflow, in.PolicyVersionID, p.UserID).Scan(&id)
	if err == nil {
		err = a.writeAudit(ctx, tx, p, "creation_policy_binding.updated", "creation_policy_binding", id)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		catalogPromptError(w, err)
		return
	}
	catalogComplete(w, 200, map[string]any{"id": id, "workflow": workflow, "policy_version_id": v.VersionID, "content_hash": v.ContentHash})
}
