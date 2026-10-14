package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"sort"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
)

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

func (a *PromptRepository) executePrompt(w *catalogResult, r *catalogRequest, p Principal, tx pgx.Tx, parts []string) {
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

type promptPackWrite struct {
	Key             string             `json:"key"`
	Name            string             `json:"name"`
	Description     *string            `json:"description"`
	Entries         *[]PromptPackEntry `json:"entries"`
	ExpectedVersion int                `json:"expected_version"`
	VersionID       string             `json:"version_id"`
	Workflow        string             `json:"workflow"`
}

func validatePromptPack(ctx context.Context, tx pgx.Tx, workspace string, entries []PromptPackEntry, published bool) error {
	if len(entries) == 0 {
		return promptInvalid("prompt pack entries must not be empty")
	}
	nodes := map[string]bool{}
	for _, entry := range entries {
		if nodes[entry.NodeKey] {
			return promptInvalid("conflicting prompt entries for node %s", entry.NodeKey)
		}
		nodes[entry.NodeKey] = true
		if _, err := uuid.Parse(entry.PromptVersionID); err != nil {
			return promptInvalid("invalid prompt version ID")
		}
		v, err := scanPrompt(tx.QueryRow(ctx, promptSelect+`WHERE v.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.status='published'`, workspace, entry.PromptVersionID))
		if err != nil {
			if errors.Is(err, pgx.ErrNoRows) {
				return promptInvalid("prompt version must be published and in workspace scope")
			}
			return err
		}
		if v.NodeKey != entry.NodeKey {
			return promptInvalid("prompt version does not match node %s", entry.NodeKey)
		}
		if err = validatePrompt(ctx, tx, v); err != nil {
			return err
		}
	}
	if published {
		complete := false
		for _, workflow := range []string{"novel.write@1", "content.generate@1", "memory.refresh@1", "media.video@1", "media.image@1", "media.speech@1", "media.music@1", "media.sound_effect@1", "media.storyboard@1", "media.timeline@1", "media.compose@1"} {
			required, _ := workflowPromptNodes(workflow)
			if len(required) != len(nodes) {
				continue
			}
			match := true
			for _, node := range required {
				if !nodes[node] {
					match = false
				}
			}
			if match {
				complete = true
			}
		}
		if !complete {
			return promptInvalid("published prompt pack must contain a complete workflow node set")
		}
	}
	return nil
}

func (a *PromptRepository) executePack(w *catalogResult, r *catalogRequest, p Principal, tx pgx.Tx, parts []string) {
	ctx := r.Context()
	var err error
	if len(parts) == 0 && r.action == governance.Read {
		rows, e := tx.Query(ctx, packSelect+`WHERE (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version ORDER BY p.key,p.workspace_id NULLS LAST`, p.WorkspaceID)
		if e != nil {
			catalogPromptError(w, e)
			return
		}
		items := []PromptPackVersion{}
		for rows.Next() {
			v, e := scanPromptPack(rows)
			if e != nil {
				rows.Close()
				catalogPromptError(w, e)
				return
			}
			if status := r.filters["status"]; status == "" || status == v.Status {
				items = append(items, v)
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		for i := range items {
			items[i].Entries, err = promptPackEntries(ctx, tx, items[i].VersionID)
			if err != nil {
				catalogPromptError(w, err)
				return
			}
		}
		catalogComplete(w, 200, map[string]any{"items": items, "has_more": false})
		return
	}
	if len(parts) > 3 {
		catalogNotFound(w)
		return
	}
	var current PromptPackVersion
	action := ""
	if len(parts) > 0 {
		if _, e := uuid.Parse(parts[0]); e != nil {
			catalogReject(w, 404, "prompt pack not found")
			return
		}
		current, err = scanPromptPack(tx.QueryRow(ctx, packSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version`, p.WorkspaceID, parts[0]))
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		if len(parts) > 1 {
			action = parts[1]
		}
		if r.action == governance.Read && action == "versions" && len(parts) == 2 {
			rows, e := tx.Query(ctx, packSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) ORDER BY v.version DESC`, p.WorkspaceID, current.ID)
			if e != nil {
				catalogPromptError(w, e)
				return
			}
			items := []PromptPackVersion{}
			for rows.Next() {
				v, e := scanPromptPack(rows)
				if e != nil {
					rows.Close()
					catalogPromptError(w, e)
					return
				}
				items = append(items, v)
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				catalogPromptError(w, err)
				return
			}
			for i := range items {
				items[i].Entries, err = promptPackEntries(ctx, tx, items[i].VersionID)
				if err != nil {
					catalogPromptError(w, err)
					return
				}
			}
			catalogComplete(w, 200, map[string]any{"items": items})
			return
		}
		if r.action == governance.Read && action == "versions" && len(parts) == 3 {
			current, err = scanPromptPack(tx.QueryRow(ctx, packSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND (v.id::text=$3 OR v.version::text=$3)`, p.WorkspaceID, current.ID, parts[2]))
			if err != nil {
				catalogPromptError(w, err)
				return
			}
		} else if r.action == governance.Read && action != "" {
			catalogNotFound(w)
			return
		}
		current.Entries, err = promptPackEntries(ctx, tx, current.VersionID)
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		if r.action == governance.Read {
			catalogComplete(w, 200, current)
			return
		}
	}
	if len(parts) == 3 {
		catalogNotFound(w)
		return
	}
	clone := action == "clone" && r.action == governance.Create
	creating := len(parts) == 0 || clone
	if len(parts) > 0 && current.WorkspaceID == nil && !clone {
		catalogReject(w, 409, "platform prompt packs must be cloned before editing")
		return
	}
	if !(len(parts) == 0 && r.action == governance.Create || len(parts) == 1 && (r.action == governance.Replace || r.action == governance.Update || r.action == governance.Delete) || len(parts) == 2 && r.action == governance.Create && (action == "versions" || action == "publish" || action == "disable" || action == "clone")) {
		catalogUnsupported(w)
		return
	}
	in := promptPackWrite{}
	if r.action != governance.Delete {
		if err = decodeCatalogBody(r, &in); err != nil {
			catalogPromptError(w, err)
			return
		}
	}
	if !creating {
		var locked int
		if err = tx.QueryRow(ctx, `SELECT current_version FROM prompt_packs WHERE id=$2 AND workspace_id=$1 FOR UPDATE`, p.WorkspaceID, current.ID).Scan(&locked); err != nil {
			catalogPromptError(w, err)
			return
		}
		if locked != current.Version {
			current, err = scanPromptPack(tx.QueryRow(ctx, packSelect+`WHERE p.id=$2 AND p.workspace_id=$1 AND v.version=p.current_version`, p.WorkspaceID, current.ID))
			if err != nil {
				catalogPromptError(w, err)
				return
			}
			current.Entries, err = promptPackEntries(ctx, tx, current.VersionID)
			if err != nil {
				catalogPromptError(w, err)
				return
			}
		}
		if in.ExpectedVersion != 0 && in.ExpectedVersion != current.Version {
			catalogReject(w, 409, "prompt pack version conflict")
			return
		}
	}
	next := current
	if in.VersionID != "" {
		if len(parts) == 0 {
			catalogReject(w, 422, "source version requires pack identity")
			return
		}
		next, err = scanPromptPack(tx.QueryRow(ctx, packSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.id::text=$3`, p.WorkspaceID, current.ID, in.VersionID))
		if err != nil {
			catalogPromptError(w, err)
			return
		}
		next.Entries, err = promptPackEntries(ctx, tx, next.VersionID)
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
	if in.Description != nil {
		next.Description = *in.Description
	}
	if in.Entries != nil {
		next.Entries = *in.Entries
	}
	if creating {
		next.ID = uuid.NewString()
		next.Version = 1
		next.WorkspaceID = &p.WorkspaceID
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
	if action == "disable" || r.action == governance.Delete {
		next.Status = "disabled"
	}
	if err = validatePromptPack(ctx, tx, p.WorkspaceID, next.Entries, next.Status == "published"); err != nil {
		catalogPromptError(w, err)
		return
	}
	sort.Slice(next.Entries, func(i, j int) bool { return next.Entries[i].NodeKey < next.Entries[j].NodeKey })
	bindings := map[string]string{}
	for _, entry := range next.Entries {
		bindings[entry.NodeKey] = entry.PromptVersionID
	}
	raw, e := canonicalPromptJSON(bindings)
	if e != nil {
		catalogPromptError(w, e)
		return
	}
	next.ContentHash = shaPromptBytes(raw)
	if creating {
		_, err = tx.Exec(ctx, `INSERT INTO prompt_packs(id,workspace_id,key,name,description,current_version) VALUES($1,$2,$3,$4,$5,$6)`, next.ID, p.WorkspaceID, next.Key, next.Name, next.Description, next.Version)
	} else {
		_, err = tx.Exec(ctx, `UPDATE prompt_packs SET key=$3,name=$4,description=$5,current_version=$6 WHERE id=$2 AND workspace_id=$1`, p.WorkspaceID, next.ID, next.Key, next.Name, next.Description, next.Version)
	}
	if err != nil {
		catalogPromptError(w, err)
		return
	}
	next.VersionID = uuid.NewString()
	err = tx.QueryRow(ctx, `INSERT INTO prompt_pack_versions(id,pack_id,workspace_id,version,status,content_hash,created_by) VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING created_at`, next.VersionID, next.ID, p.WorkspaceID, next.Version, next.Status, next.ContentHash, p.UserID).Scan(&next.CreatedAt)
	for _, entry := range next.Entries {
		if err != nil {
			break
		}
		_, err = tx.Exec(ctx, `INSERT INTO prompt_pack_entries(pack_version_id,node_key,prompt_version_id) VALUES($1,$2,$3)`, next.VersionID, entry.NodeKey, entry.PromptVersionID)
	}
	if err == nil && next.Status == "published" && in.Workflow != "" {
		_, err = validatePackForWorkflow(ctx, tx, p.WorkspaceID, next.VersionID, in.Workflow)
	}
	if err == nil {
		err = a.writeAudit(ctx, tx, p, "prompt_pack."+next.Status, "prompt_pack", next.ID)
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

type workflowPromptBinding struct {
	ID            string  `json:"id"`
	WorkspaceID   *string `json:"workspace_id"`
	Workflow      string  `json:"workflow"`
	PackVersionID string  `json:"pack_version_id"`
	CreatedAt     any     `json:"created_at"`
}

func (a *PromptRepository) executeBinding(w *catalogResult, r *catalogRequest, p Principal, tx pgx.Tx, parts []string) {
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
