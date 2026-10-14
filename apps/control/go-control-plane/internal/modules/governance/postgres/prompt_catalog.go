package postgres

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

const promptSelect = `SELECT p.id::text,v.id::text,p.workspace_id::text,p.key,p.node_key,p.name,p.description,v.version,v.messages,v.variables_schema,v.output_schema,v.renderer_version,v.status,v.content_hash,v.created_at FROM prompts p JOIN prompt_versions v ON v.prompt_id=p.id `

func scanPrompt(row pgx.Row) (PromptVersion, error) {
	var v PromptVersion
	var messages []byte
	err := row.Scan(&v.ID, &v.VersionID, &v.WorkspaceID, &v.Key, &v.NodeKey, &v.Name, &v.Description, &v.Version, &messages, &v.VariablesSchema, &v.OutputSchema, &v.RendererVersion, &v.Status, &v.ContentHash, &v.CreatedAt)
	if err == nil {
		err = json.Unmarshal(messages, &v.Messages)
	}
	return v, err
}

const packSelect = `SELECT p.id::text,v.id::text,p.workspace_id::text,p.key,p.name,p.description,v.version,v.status,v.content_hash,v.created_at FROM prompt_packs p JOIN prompt_pack_versions v ON v.pack_id=p.id `

func scanPromptPack(row pgx.Row) (PromptPackVersion, error) {
	var v PromptPackVersion
	err := row.Scan(&v.ID, &v.VersionID, &v.WorkspaceID, &v.Key, &v.Name, &v.Description, &v.Version, &v.Status, &v.ContentHash, &v.CreatedAt)
	return v, err
}

func promptPackEntries(ctx context.Context, tx pgx.Tx, id string) ([]PromptPackEntry, error) {
	rows, err := tx.Query(ctx, `SELECT node_key,prompt_version_id::text FROM prompt_pack_entries WHERE pack_version_id=$1 ORDER BY node_key`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := []PromptPackEntry{}
	for rows.Next() {
		var e PromptPackEntry
		if err = rows.Scan(&e.NodeKey, &e.PromptVersionID); err != nil {
			return nil, err
		}
		result = append(result, e)
	}
	return result, rows.Err()
}

func (a *PromptRepository) FrozenVersion(ctx context.Context, workspace, id string) (PromptVersion, error) {
	if _, err := uuid.Parse(id); err != nil {
		return PromptVersion{}, promptInvalid("invalid prompt version ID")
	}
	tx, err := workspaceTx(ctx, a.pool, workspace)
	if err != nil {
		return PromptVersion{}, err
	}
	defer tx.Rollback(ctx)
	// A disabled current identity does not revoke an older published immutable version.
	v, err := scanPrompt(tx.QueryRow(ctx, promptSelect+`WHERE v.id=$2 AND (p.workspace_id IS NULL OR p.workspace_id=$1) AND v.status='published'`, workspace, id))
	if err == nil {
		err = validatePrompt(ctx, tx, v)
	}
	if err == nil {
		err = verifyPromptHash(v)
	}
	return v, err
}

func validatePackForWorkflow(ctx context.Context, tx pgx.Tx, workspace, id, workflow string) (FrozenPromptBinding, error) {
	required, err := workflowPromptNodes(workflow)
	if err != nil {
		return FrozenPromptBinding{}, err
	}
	var packID, contentHash string
	err = tx.QueryRow(ctx, `SELECT id::text,content_hash FROM prompt_pack_versions WHERE id=$2 AND (workspace_id IS NULL OR workspace_id=$1) AND status='published'`, workspace, id).Scan(&packID, &contentHash)
	if err != nil {
		return FrozenPromptBinding{}, err
	}
	entries, err := promptPackEntries(ctx, tx, id)
	if err != nil {
		return FrozenPromptBinding{}, err
	}
	bindings := map[string]string{}
	for _, entry := range entries {
		v, e := scanPrompt(tx.QueryRow(ctx, promptSelect+`WHERE v.id=$2 AND (p.workspace_id IS NULL OR p.workspace_id=$1) AND v.status='published'`, workspace, entry.PromptVersionID))
		if e != nil {
			return FrozenPromptBinding{}, e
		}
		if v.NodeKey != entry.NodeKey {
			return FrozenPromptBinding{}, promptInvalid("prompt node mismatch")
		}
		if e = validatePrompt(ctx, tx, v); e != nil {
			return FrozenPromptBinding{}, e
		}
		if e = verifyPromptHash(v); e != nil {
			return FrozenPromptBinding{}, e
		}
		bindings[entry.NodeKey] = entry.PromptVersionID
	}
	canonical, err := canonicalPromptJSON(bindings)
	if err != nil {
		return FrozenPromptBinding{}, err
	}
	if shaPromptBytes(canonical) != contentHash {
		return FrozenPromptBinding{}, promptInvalid("prompt pack content hash mismatch")
	}
	frozen := map[string]string{}
	for _, node := range required {
		id, ok := bindings[node]
		if !ok {
			return FrozenPromptBinding{}, promptInvalid("prompt pack missing node %s", node)
		}
		frozen[node] = id
	}
	if workflow != "memory.refresh@1" && len(bindings) != len(required) {
		return FrozenPromptBinding{}, promptInvalid("prompt pack contains nodes outside workflow")
	}
	return FrozenPromptBinding{PackVersionID: packID, Bindings: frozen}, nil
}

func (a *PromptRepository) Freeze(ctx context.Context, workspace, workflow string) (FrozenPromptBinding, error) {
	tx, err := workspaceTx(ctx, a.pool, workspace)
	if err != nil {
		return FrozenPromptBinding{}, err
	}
	defer tx.Rollback(ctx)
	var id string
	err = tx.QueryRow(ctx, `SELECT pack_version_id::text FROM workflow_prompt_bindings WHERE workflow=$2 AND (workspace_id=$1 OR workspace_id IS NULL) ORDER BY workspace_id NULLS LAST LIMIT 1`, workspace, workflow).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return FrozenPromptBinding{}, promptInvalid("workflow %s has no explicit prompt binding", workflow)
	}
	if err != nil {
		return FrozenPromptBinding{}, err
	}
	return validatePackForWorkflow(ctx, tx, workspace, id, workflow)
}

func validatePrompt(ctx context.Context, tx pgx.Tx, v PromptVersion) error {
	var variables, output json.RawMessage
	var renderer string
	if err := tx.QueryRow(ctx, `SELECT variables_schema,output_schema,renderer_version FROM prompt_node_contracts WHERE node_key=$1`, v.NodeKey).Scan(&variables, &output, &renderer); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return promptInvalid("unknown node %s", v.NodeKey)
		}
		return err
	}
	return validatePromptContract(v, variables, output, renderer)
}
