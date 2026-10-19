package prompts

import (
	"encoding/json"
	"strings"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

// ============================================================================
// Prompt Catalog - Write Operations (Prompt resources)
// ============================================================================

type promptWrite struct {
	Key             string           `json:"key"`
	NodeKey         string           `json:"node_key"`
	Name            string           `json:"name"`
	Description     *string          `json:"description"`
	Messages        *[]PromptMessage `json:"messages"`
	VariablesSchema json.RawMessage  `json:"variables_schema"`
	OutputSchema    json.RawMessage  `json:"output_schema"`
	RendererVersion string           `json:"renderer_version"`
	ExpectedVersion int              `json:"expected_version"`
	VersionID       string           `json:"version_id"`
}

func applyPromptWrite(v PromptVersion, in promptWrite) PromptVersion {
	if in.Key != "" {
		v.Key = in.Key
	}
	if in.NodeKey != "" {
		v.NodeKey = in.NodeKey
	}
	if in.Name != "" {
		v.Name = in.Name
	}
	if in.Description != nil {
		v.Description = *in.Description
	}
	if in.Messages != nil {
		v.Messages = *in.Messages
	}
	if len(in.VariablesSchema) > 0 {
		v.VariablesSchema = in.VariablesSchema
	}
	if len(in.OutputSchema) > 0 {
		v.OutputSchema = in.OutputSchema
	}
	if in.RendererVersion != "" {
		v.RendererVersion = in.RendererVersion
	}
	return v
}

func (a *Repository) executePrompt(w *catalogResult, r *catalogRequest, p Principal, tx pgx.Tx, parts []string) {
	ctx := r.Context()
	if len(parts) == 0 && r.action == governance.Read {
		rows, err := tx.Query(ctx, promptSelect+`WHERE (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version ORDER BY p.key,p.workspace_id NULLS LAST`, p.WorkspaceID)
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		defer rows.Close()
		items := []PromptVersion{}
		for rows.Next() {
			v, e := scanPrompt(rows)
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
	if len(parts) > 3 {
		catalogNotFound(w)
		return
	}
	var current PromptVersion
	var err error
	action := ""
	if len(parts) > 0 {
		if _, e := uuid.Parse(parts[0]); e != nil {
			catalogReject(w, 404, "prompt not found")
			return
		}
		current, err = scanPrompt(tx.QueryRow(ctx, promptSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version`, p.WorkspaceID, parts[0]))
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		if len(parts) > 1 {
			action = parts[1]
		}
		if r.action == governance.Read {
			if action == "versions" && len(parts) == 2 {
				rows, e := tx.Query(ctx, promptSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) ORDER BY v.version DESC`, p.WorkspaceID, current.ID)
				if e != nil {
					catalogPromptError(w, e)
					return
				}
				defer rows.Close()
				items := []PromptVersion{}
				for rows.Next() {
					v, e := scanPrompt(rows)
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
				current, err = scanPrompt(tx.QueryRow(ctx, promptSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND (v.id::text=$3 OR v.version::text=$3)`, p.WorkspaceID, current.ID, parts[2]))
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
	if action == "preview" && r.action == governance.Create && len(parts) == 2 {
		var in struct {
			Variables map[string]any `json:"variables"`
			VersionID string         `json:"version_id"`
		}
		if err = decodeCatalogBody(r, &in); err != nil {
			catalogPromptError(w, err)
			return
		}
		if in.VersionID != "" {
			current, err = scanPrompt(tx.QueryRow(ctx, promptSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.id::text=$3`, p.WorkspaceID, current.ID, in.VersionID))
			if err != nil {
				catalogPromptError(w, err)
				return
			}
		}
		messages, e := renderPromptPreview(current, in.Variables)
		if e != nil {
			catalogPromptError(w, e)
			return
		}
		catalogComplete(w, 200, map[string]any{"messages": messages, "version_id": current.VersionID})
		return
	}
	if len(parts) == 3 {
		catalogNotFound(w)
		return
	}
	clone := action == "clone" && r.action == governance.Create
	if len(parts) > 0 && current.WorkspaceID == nil && !clone {
		catalogReject(w, 409, "platform prompts must be cloned before editing")
		return
	}
	if !(len(parts) == 0 && r.action == governance.Create || len(parts) == 1 && (r.action == governance.Replace || r.action == governance.Update || r.action == governance.Delete) || len(parts) == 2 && r.action == governance.Create && (action == "versions" || action == "publish" || action == "disable" || action == "clone")) {
		catalogUnsupported(w)
		return
	}
	in := promptWrite{}
	if r.action != governance.Delete {
		if err = decodeCatalogBody(r, &in); err != nil {
			catalogPromptError(w, err)
			return
		}
	}
	creating := len(parts) == 0 || clone
	if !creating {
		var locked int
		if err = tx.QueryRow(ctx, `SELECT current_version FROM prompts WHERE id=$2 AND workspace_id=$1 FOR UPDATE`, p.WorkspaceID, current.ID).Scan(&locked); err != nil {
			catalogPromptError(w, err)
			return
		}
		if locked != current.Version {
			current, err = scanPrompt(tx.QueryRow(ctx, promptSelect+`WHERE p.id=$2 AND p.workspace_id=$1 AND v.version=p.current_version`, p.WorkspaceID, current.ID))
			if err != nil {
				catalogPromptError(w, err)
				return
			}
		}
		if in.ExpectedVersion != 0 && in.ExpectedVersion != current.Version {
			catalogReject(w, 409, "prompt version conflict")
			return
		}
	}
	source := current
	if in.VersionID != "" {
		if len(parts) == 0 {
			catalogReject(w, 422, "source version requires a prompt identity")
			return
		}
		source, err = scanPrompt(tx.QueryRow(ctx, promptSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.id::text=$3`, p.WorkspaceID, current.ID, in.VersionID))
		if err != nil {
			catalogPromptError(w, err)
			return
		}
	}
	next := applyPromptWrite(source, in)
	if creating {
		next.ID = uuid.NewString()
		next.WorkspaceID = &p.WorkspaceID
		next.Version = 1
		if clone && in.Key == "" {
			catalogReject(w, 422, "clone key required")
			return
		}
	} else {
		next.Version = current.Version + 1
		next.ID = current.ID
		next.WorkspaceID = current.WorkspaceID
		if next.NodeKey != current.NodeKey {
			catalogReject(w, 422, "prompt node identity cannot change")
			return
		}
	}
	next.Status = "draft"
	if action == "publish" {
		next.Status = "published"
	}
	if action == "disable" || r.action == governance.Delete {
		next.Status = "disabled"
	}
	if strings.TrimSpace(next.Key) == "" || strings.TrimSpace(next.Name) == "" {
		catalogReject(w, 422, "key and name required")
		return
	}
	if err = validatePrompt(ctx, tx, next); err != nil {
		catalogPromptError(w, err)
		return
	}
	next.ContentHash, err = promptHash(next)
	if err != nil {
		catalogPromptError(w, err)
		return
	}
	if creating {
		_, err = tx.Exec(ctx, `INSERT INTO prompts(id,workspace_id,key,node_key,name,description,current_version) VALUES($1,$2,$3,$4,$5,$6,$7)`, next.ID, p.WorkspaceID, next.Key, next.NodeKey, next.Name, next.Description, next.Version)
	} else {
		_, err = tx.Exec(ctx, `UPDATE prompts SET key=$3,name=$4,description=$5,current_version=$6 WHERE id=$2 AND workspace_id=$1`, p.WorkspaceID, next.ID, next.Key, next.Name, next.Description, next.Version)
	}
	if err != nil {
		catalogPromptError(w, err)
		return
	}
	next.VersionID = uuid.NewString()
	messages, _ := json.Marshal(next.Messages)
	err = tx.QueryRow(ctx, `INSERT INTO prompt_versions(id,prompt_id,workspace_id,version,messages,variables_schema,output_schema,renderer_version,status,content_hash,created_by) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING created_at`, next.VersionID, next.ID, p.WorkspaceID, next.Version, messages, next.VariablesSchema, next.OutputSchema, next.RendererVersion, next.Status, next.ContentHash, p.UserID).Scan(&next.CreatedAt)
	if err == nil {
		err = a.writeAudit(ctx, tx, p, "prompt."+next.Status, "prompt", next.ID)
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
