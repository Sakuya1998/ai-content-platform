package prompts

import (
	"context"
	"errors"
	"sort"
	"strings"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

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

func (a *Repository) executePack(w *catalogResult, r *catalogRequest, p Principal, tx pgx.Tx, parts []string) {
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
