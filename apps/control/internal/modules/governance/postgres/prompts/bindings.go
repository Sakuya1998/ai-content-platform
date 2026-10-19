package prompts

import (
	"errors"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

type workflowPromptBinding struct {
	ID            string  `json:"id"`
	WorkspaceID   *string `json:"workspace_id"`
	Workflow      string  `json:"workflow"`
	PackVersionID string  `json:"pack_version_id"`
	CreatedAt     any     `json:"created_at"`
}

func (a *Repository) executeBinding(w *catalogResult, r *catalogRequest, p Principal, tx pgx.Tx, parts []string) {
	ctx := r.Context()
	if len(parts) > 2 {
		catalogNotFound(w)
		return
	}
	workflow := ""
	if len(parts) > 0 {
		workflow = parts[0]
		if _, err := workflowPromptNodes(workflow); err != nil {
			catalogPromptError(w, err)
			return
		}
	}
	if r.action == governance.Read {
		query := `SELECT id::text,workspace_id::text,workflow,pack_version_id::text,created_at FROM workflow_prompt_bindings WHERE (workspace_id=$1 OR workspace_id IS NULL)`
		args := []any{p.WorkspaceID}
		if len(parts) == 2 {
			if parts[1] != "versions" && parts[1] != "history" {
				catalogNotFound(w)
				return
			}
			query = `SELECT id::text,workspace_id::text,workflow,pack_version_id::text,created_at FROM workflow_prompt_binding_history WHERE (workspace_id=$1 OR workspace_id IS NULL)`
		}
		if workflow != "" {
			query += ` AND workflow=$2`
			args = append(args, workflow)
		}
		query += ` ORDER BY workflow,workspace_id NULLS LAST,created_at DESC`
		rows, err := tx.Query(ctx, query, args...)
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		defer rows.Close()
		items := []workflowPromptBinding{}
		for rows.Next() {
			var v workflowPromptBinding
			if err = rows.Scan(&v.ID, &v.WorkspaceID, &v.Workflow, &v.PackVersionID, &v.CreatedAt); err != nil {
				catalogPromptError(w, err)
				return
			}
			items = append(items, v)
		}
		if err = rows.Err(); err != nil {
			catalogPromptError(w, err)
			return
		}
		catalogComplete(w, 200, map[string]any{"items": items})
		return
	}
	if r.action == governance.Delete && len(parts) == 1 {
		tag, err := tx.Exec(ctx, `DELETE FROM workflow_prompt_bindings WHERE workspace_id=$1 AND workflow=$2`, p.WorkspaceID, workflow)
		if err == nil && tag.RowsAffected() == 0 {
			err = pgx.ErrNoRows
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
	if r.action != governance.Create && r.action != governance.Replace && r.action != governance.Update {
		catalogUnsupported(w)
		return
	}
	if len(parts) == 2 && parts[1] != "rollback" {
		catalogNotFound(w)
		return
	}
	var in struct {
		Workflow              string `json:"workflow"`
		PackVersionID         string `json:"pack_version_id"`
		HistoryID             string `json:"history_id"`
		ExpectedPackVersionID string `json:"expected_pack_version_id"`
	}
	if err := decodeCatalogBody(r, &in); err != nil {
		catalogPromptError(w, err)
		return
	}
	if workflow == "" {
		workflow = in.Workflow
	} else if in.Workflow != "" && in.Workflow != workflow {
		catalogReject(w, 422, "workflow mismatch")
		return
	}
	if _, err := workflowPromptNodes(workflow); err != nil {
		catalogPromptError(w, err)
		return
	}
	// Serialize first-time overrides as well as updates without trusting a caller's workspace body.
	if _, err := tx.Exec(ctx, `SELECT id FROM workspaces WHERE id=$1 FOR UPDATE`, p.WorkspaceID); err != nil {
		catalogPromptError(w, err)
		return
	}
	if len(parts) == 2 {
		if in.HistoryID != "" {
			if err := tx.QueryRow(ctx, `SELECT pack_version_id::text FROM workflow_prompt_binding_history WHERE id::text=$3 AND workflow=$2 AND workspace_id=$1`, p.WorkspaceID, workflow, in.HistoryID).Scan(&in.PackVersionID); err != nil {
				catalogPromptError(w, err)
				return
			}
		} else {
			var exists bool
			if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM workflow_prompt_binding_history WHERE workflow=$2 AND (workspace_id=$1 OR workspace_id IS NULL) AND pack_version_id::text=$3)`, p.WorkspaceID, workflow, in.PackVersionID).Scan(&exists); err != nil {
				catalogPromptError(w, err)
				return
			}
			if !exists {
				catalogReject(w, 422, "rollback requires a prior workflow binding")
				return
			}
		}
	}
	if _, err := uuid.Parse(in.PackVersionID); err != nil {
		catalogReject(w, 422, "pack_version_id required")
		return
	}
	frozen, err := validatePackForWorkflow(ctx, tx, p.WorkspaceID, in.PackVersionID, workflow)
	if err != nil {
		catalogPromptError(w, err)
		return
	}
	if in.ExpectedPackVersionID != "" {
		var existing string
		err = tx.QueryRow(ctx, `SELECT pack_version_id::text FROM workflow_prompt_bindings WHERE workflow=$2 AND (workspace_id=$1 OR workspace_id IS NULL) ORDER BY workspace_id NULLS LAST LIMIT 1`, p.WorkspaceID, workflow).Scan(&existing)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			catalogPromptError(w, err)
			return
		}
		if existing != in.ExpectedPackVersionID {
			catalogReject(w, 409, "workflow binding conflict")
			return
		}
	}
	var bindingID string
	err = tx.QueryRow(ctx, `INSERT INTO workflow_prompt_bindings(workspace_id,workflow,pack_version_id,created_by) VALUES($1,$2,$3,$4) ON CONFLICT(workspace_id,workflow) DO UPDATE SET pack_version_id=EXCLUDED.pack_version_id,created_by=EXCLUDED.created_by,created_at=now() RETURNING id::text`, p.WorkspaceID, workflow, in.PackVersionID, p.UserID).Scan(&bindingID)
	if err == nil {
		err = a.writeAudit(ctx, tx, p, "workflow_prompt_binding.updated", "workflow_prompt_binding", bindingID)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		catalogPromptError(w, err)
		return
	}
	catalogComplete(w, 200, map[string]any{"id": bindingID, "workflow": workflow, "pack_version_id": frozen.PackVersionID, "bindings": frozen.Bindings})
}
