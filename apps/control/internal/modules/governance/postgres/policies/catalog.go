package policies

import (
	"context"
	"errors"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

const creationPolicySelect = `SELECT p.id::text,v.id::text,p.workspace_id::text,p.key,p.name,v.version,v.status,v.payload,v.content_hash,v.created_at FROM creation_policy_sets p JOIN creation_policy_versions v ON v.policy_set_id=p.id `

func scanCreationPolicy(row pgx.Row) (CreationPolicyVersion, error) {
	var v CreationPolicyVersion
	err := row.Scan(&v.ID, &v.VersionID, &v.WorkspaceID, &v.Key, &v.Name, &v.Version, &v.Status, &v.Payload, &v.ContentHash, &v.CreatedAt)
	return v, err
}

func creationPolicyForWorkflow(ctx context.Context, tx pgx.Tx, workspace, id, workflow string) (CreationPolicyVersion, error) {
	if _, err := uuid.Parse(id); err != nil {
		return CreationPolicyVersion{}, promptInvalid("policy_version_id must be UUID")
	}
	v, err := scanCreationPolicy(tx.QueryRow(ctx, creationPolicySelect+`WHERE v.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.status='published'`, workspace, id))
	if err == nil {
		err = verifyCreationPolicy(v, workflow)
	}
	return v, err
}

func (a *Repository) FrozenVersion(ctx context.Context, workspace, id string) (CreationPolicyVersion, error) {
	tx, err := workspaceTx(ctx, a.pool, workspace)
	if err != nil {
		return CreationPolicyVersion{}, err
	}
	defer tx.Rollback(ctx)
	return creationPolicyForWorkflow(ctx, tx, workspace, id, "")
}

func (a *Repository) Freeze(ctx context.Context, workspace, workflow string) (map[string]string, error) {
	if _, err := workflowPromptNodes(workflow); err != nil {
		return nil, err
	}
	tx, err := workspaceTx(ctx, a.pool, workspace)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	var id string
	err = tx.QueryRow(ctx, `SELECT policy_version_id::text FROM workflow_creation_policy_bindings WHERE workflow=$2 AND (workspace_id=$1 OR workspace_id IS NULL) ORDER BY workspace_id NULLS LAST LIMIT 1`, workspace, workflow).Scan(&id)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, promptInvalid("workflow %s has no creation policy binding", workflow)
	}
	if err != nil {
		return nil, err
	}
	v, err := creationPolicyForWorkflow(ctx, tx, workspace, id, workflow)
	if err != nil {
		return nil, err
	}
	return map[string]string{"version_id": v.VersionID, "content_hash": v.ContentHash}, nil
}
