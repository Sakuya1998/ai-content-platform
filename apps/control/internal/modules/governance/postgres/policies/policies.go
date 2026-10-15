package policies

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

// CreationPolicyVersion represents a versioned creation policy resource
type CreationPolicyVersion struct {
	ID          string          `json:"id"`
	VersionID   string          `json:"version_id"`
	WorkspaceID string          `json:"workspace_id,omitempty"`
	Key         string          `json:"key"`
	Version     int             `json:"version"`
	Name        string          `json:"name"`
	Payload     json.RawMessage `json:"payload"`
	Workflow    string          `json:"workflow"`
	Status      string          `json:"status"`
	ContentHash string          `json:"content_hash"`
	CreatedAt   time.Time       `json:"created_at"`
}

const creationPolicySelect = `SELECT p.id::text,v.id::text,p.workspace_id::text,p.key,v.version,v.name,v.payload,v.workflow,v.status,v.content_hash,v.created_at FROM governance_creation_policies p JOIN governance_creation_policy_versions v ON v.policy_id=p.id `

// ScanCreationPolicy reads a CreationPolicyVersion from a database row
func ScanCreationPolicy(row pgx.Row) (CreationPolicyVersion, error) {
	var v CreationPolicyVersion
	err := row.Scan(&v.ID, &v.VersionID, &v.WorkspaceID, &v.Key, &v.Version, &v.Name, &v.Payload, &v.Workflow, &v.Status, &v.ContentHash, &v.CreatedAt)
	return v, err
}

// Principal represents the authenticated user context
type Principal struct {
	WorkspaceID string
	UserID      string
}

// Repository provides creation policy CRUD operations
type Repository struct {
	pool interface {
		Begin(context.Context) (pgx.Tx, error)
	}
	writeAudit       func(context.Context, pgx.Tx, Principal, string, string, string) error
	normalizePayload func(string, json.RawMessage) (json.RawMessage, error)
	regexpKey        func(string) bool
}

// NewRepository creates a new policy repository
func NewRepository(
	pool interface {
		Begin(context.Context) (pgx.Tx, error)
	},
	writeAudit func(context.Context, pgx.Tx, Principal, string, string, string) error,
	normalizePayload func(string, json.RawMessage) (json.RawMessage, error),
	regexpKey func(string) bool,
) *Repository {
	return &Repository{
		pool:             pool,
		writeAudit:       writeAudit,
		normalizePayload: normalizePayload,
		regexpKey:        regexpKey,
	}
}

type creationPolicyWrite struct {
	Key             string          `json:"key"`
	Name            string          `json:"name"`
	Payload         json.RawMessage `json:"payload"`
	Workflow        string          `json:"workflow"`
	ExpectedVersion int             `json:"expected_version"`
}

// ExecutePolicyCatalogOp handles CRUD operations for creation policies via catalog protocol
func (repo *Repository) ExecutePolicyCatalogOp(
	ctx context.Context,
	p Principal,
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

	// List policies
	if len(parts) == 0 && action == governance.Read {
		rows, err := tx.Query(ctx, creationPolicySelect+`WHERE (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version ORDER BY p.key,p.workspace_id NULLS LAST`, p.WorkspaceID)
		if err != nil {
			return 0, nil, err
		}
		defer rows.Close()
		items := []CreationPolicyVersion{}
		for rows.Next() {
			v, e := ScanCreationPolicy(rows)
			if e != nil {
				return 0, nil, e
			}
			if status := filters["status"]; status == "" || status == v.Status {
				items = append(items, v)
			}
		}
		if err = rows.Err(); err != nil {
			return 0, nil, err
		}
		return 200, map[string]any{"items": items, "has_more": false}, tx.Commit(ctx)
	}

	// Get specific policy
	if len(parts) >= 1 && action == governance.Read {
		id := parts[0]
		if _, e := uuid.Parse(id); e != nil {
			return 404, nil, governance.ErrNotFound
		}

		if len(parts) == 2 && parts[1] == "versions" {
			rows, e := tx.Query(ctx, creationPolicySelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) ORDER BY v.version DESC`, p.WorkspaceID, id)
			if e != nil {
				return 0, nil, e
			}
			defer rows.Close()
			items := []CreationPolicyVersion{}
			for rows.Next() {
				v, e := ScanCreationPolicy(rows)
				if e != nil {
					return 0, nil, e
				}
				items = append(items, v)
			}
			return 200, map[string]any{"items": items}, tx.Commit(ctx)
		}

		current, err := ScanCreationPolicy(tx.QueryRow(ctx, creationPolicySelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version`, p.WorkspaceID, id))
		if err != nil {
			return 0, nil, err
		}
		return 200, current, tx.Commit(ctx)
	}

	// Create or update policy
	var input creationPolicyWrite
	if len(body) > 0 {
		if err := json.Unmarshal(body, &input); err != nil {
			return 422, nil, err
		}
	}

	// Create new policy
	if len(parts) == 0 && action == governance.Create {
		if !repo.regexpKey(input.Key) {
			return 422, nil, governance.ErrInvalidKey
		}
		if strings.TrimSpace(input.Name) == "" {
			return 422, nil, governance.ErrNameRequired
		}
		if input.Workflow == "" {
			return 422, nil, governance.ErrWorkflowRequired
		}

		id := uuid.NewString()
		versionID := uuid.NewString()

		payload := input.Payload
		if len(payload) == 0 {
			payload = json.RawMessage(`{}`)
		}

		normalized, err := repo.normalizePayload("creation_policy", payload)
		if err != nil {
			return 422, nil, err
		}

		digest := sha256.Sum256(normalized)
		contentHash := hex.EncodeToString(digest[:])

		v := CreationPolicyVersion{
			ID:          id,
			VersionID:   versionID,
			WorkspaceID: p.WorkspaceID,
			Key:         input.Key,
			Version:     1,
			Name:        input.Name,
			Payload:     normalized,
			Workflow:    input.Workflow,
			Status:      "draft",
			ContentHash: contentHash,
			CreatedAt:   time.Now().UTC(),
		}

		_, err = tx.Exec(ctx, "INSERT INTO governance_creation_policies(id,workspace_id,key,current_version) VALUES($1,$2,$3,1)", id, p.WorkspaceID, input.Key)
		if err == nil {
			_, err = tx.Exec(ctx, "INSERT INTO governance_creation_policy_versions(id,policy_id,version,name,payload,workflow,status,content_hash,created_by,created_at) VALUES($1,$2,1,$3,$4,$5,'draft',$6,$7,$8)",
				versionID, id, v.Name, normalized, v.Workflow, contentHash, p.UserID, v.CreatedAt)
		}
		if err == nil {
			err = repo.writeAudit(ctx, tx, p, "governance.policy.created", "policies", id)
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			return 0, nil, err
		}
		return 201, v, nil
	}

	// Update existing policy
	if len(parts) == 1 && (action == governance.Update || action == governance.Replace) {
		id := parts[0]
		if _, e := uuid.Parse(id); e != nil {
			return 404, nil, governance.ErrNotFound
		}

		// Lock policy for update
		err = tx.QueryRow(ctx, "SELECT id::text FROM governance_creation_policies WHERE workspace_id=$1 AND id::text=$2 FOR UPDATE", p.WorkspaceID, id).Scan(&id)
		if err != nil {
			return 0, nil, err
		}

		current, err := ScanCreationPolicy(tx.QueryRow(ctx, creationPolicySelect+`WHERE p.id=$2 AND p.workspace_id=$1 AND v.version=p.current_version`, p.WorkspaceID, id))
		if err != nil {
			return 0, nil, err
		}

		if input.ExpectedVersion > 0 && input.ExpectedVersion != current.Version {
			return 409, nil, governance.ErrConflict
		}

		versionID := uuid.NewString()
		next := current
		next.Version++
		next.VersionID = versionID
		next.Status = "draft"
		next.CreatedAt = time.Now().UTC()

		if input.Name != "" {
			next.Name = input.Name
		}
		if len(input.Payload) > 0 {
			normalized, err := repo.normalizePayload("creation_policy", input.Payload)
			if err != nil {
				return 422, nil, err
			}
			next.Payload = normalized
		}
		if input.Workflow != "" {
			next.Workflow = input.Workflow
		}

		digest := sha256.Sum256(next.Payload)
		next.ContentHash = hex.EncodeToString(digest[:])

		_, err = tx.Exec(ctx, "INSERT INTO governance_creation_policy_versions(id,policy_id,version,name,payload,workflow,status,content_hash,created_by,created_at) VALUES($1,$2,$3,$4,$5,$6,'draft',$7,$8,$9)",
			versionID, id, next.Version, next.Name, next.Payload, next.Workflow, next.ContentHash, p.UserID, next.CreatedAt)
		if err == nil {
			_, err = tx.Exec(ctx, "UPDATE governance_creation_policies SET current_version=$3 WHERE workspace_id=$1 AND id=$2", p.WorkspaceID, id, next.Version)
		}
		if err == nil {
			err = repo.writeAudit(ctx, tx, p, "governance.policy.updated", "policies", id)
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			return 0, nil, err
		}
		return 200, next, nil
	}

	// Publish policy
	if len(parts) == 2 && parts[1] == "publish" && action == governance.Create {
		id := parts[0]
		if _, e := uuid.Parse(id); e != nil {
			return 404, nil, governance.ErrNotFound
		}

		// Lock policy
		err = tx.QueryRow(ctx, "SELECT id::text FROM governance_creation_policies WHERE workspace_id=$1 AND id::text=$2 FOR UPDATE", p.WorkspaceID, id).Scan(&id)
		if err != nil {
			return 0, nil, err
		}

		current, err := ScanCreationPolicy(tx.QueryRow(ctx, creationPolicySelect+`WHERE p.id=$2 AND p.workspace_id=$1 AND v.version=p.current_version`, p.WorkspaceID, id))
		if err != nil {
			return 0, nil, err
		}

		if current.Status == "published" {
			return 200, current, tx.Commit(ctx)
		}

		versionID := uuid.NewString()
		next := current
		next.Version++
		next.VersionID = versionID
		next.Status = "published"
		next.CreatedAt = time.Now().UTC()

		_, err = tx.Exec(ctx, "INSERT INTO governance_creation_policy_versions(id,policy_id,version,name,payload,workflow,status,content_hash,created_by,created_at) VALUES($1,$2,$3,$4,$5,$6,'published',$7,$8,$9)",
			versionID, id, next.Version, next.Name, next.Payload, next.Workflow, next.ContentHash, p.UserID, next.CreatedAt)
		if err == nil {
			_, err = tx.Exec(ctx, "UPDATE governance_creation_policies SET current_version=$3 WHERE workspace_id=$1 AND id=$2", p.WorkspaceID, id, next.Version)
		}
		if err == nil {
			err = repo.writeAudit(ctx, tx, p, "governance.policy.published", "policies", id)
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			return 0, nil, err
		}
		return 200, next, nil
	}

	return 405, nil, governance.ErrMethodNotAllowed
}

// FrozenPolicy retrieves a specific frozen policy version by ID
func (repo *Repository) FrozenPolicy(ctx context.Context, workspace, id string) (CreationPolicyVersion, error) {
	tx, err := repo.pool.Begin(ctx)
	if err != nil {
		return CreationPolicyVersion{}, err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, "SET LOCAL rls.workspace_id = $1", workspace)
	if err != nil {
		return CreationPolicyVersion{}, err
	}
	return ScanCreationPolicy(tx.QueryRow(ctx, creationPolicySelect+"WHERE v.id::text=$1", id))
}
