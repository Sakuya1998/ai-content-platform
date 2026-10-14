// Package postgres implements tenant-scoped project persistence.
package postgres

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/project"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/database"
)

type Repository struct {
	Pool                *pgxpool.Pool
	Conflict            error
	Audit               func(context.Context, pgx.Tx, string, string, string) error
	MarkCandidatesStale func(context.Context, pgx.Tx, string, string) (int64, error)
	CreateInitialDocument func(context.Context, pgx.Tx, string, string, string, string, string) error
}

func text(value any) string {
	result, _ := value.(string)
	return result
}

func (r Repository) Create(ctx context.Context, scope string, input map[string]any, key string) (project.Resource, error) {
	var result project.Resource
	if r.Pool == nil {
		return result, errors.New("project database unavailable")
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.Pool, scope)
	if err != nil {
		return result, err
	}
	defer tx.Rollback(ctx)
	result, err = r.CreateWithin(ctx, tx, scope, input, key)
	if err != nil {
		return result, err
	}
	return result, tx.Commit(ctx)
}

func Load(ctx context.Context, tx pgx.Tx, scope, id string, lock bool) (project.Resource, error) {
	var result project.Resource
	var input, state []byte
	query := `SELECT n.id,n.workspace_id,n.document_id,p.name,n.input,n.state,n.state_hash,n.brief_version,n.revision,p.content_type FROM project_projections n JOIN projects p ON p.id=n.id AND p.workspace_id=n.workspace_id WHERE n.id=$1 AND n.workspace_id=$2`
	if lock {
		query += " FOR UPDATE OF n"
	}
	err := tx.QueryRow(ctx, query, id, scope).Scan(&result.ID, &result.WorkspaceID, &result.DocumentID, &result.Title, &input, &state, &result.StateHash, &result.BriefVersion, &result.Revision, &result.ContentType)
	if err != nil {
		return result, err
	}
	if err = json.Unmarshal(input, &result.Input); err != nil {
		return result, err
	}
	err = json.Unmarshal(state, &result.State)
	return result, err
}

func (r Repository) CreateWithin(ctx context.Context, tx pgx.Tx, scope string, input map[string]any, key string) (project.Resource, error) {
	body, _ := json.Marshal(input)
	sum := sha256.Sum256(body)
	fingerprint := hex.EncodeToString(sum[:])
	id, doc := uuid.NewString(), uuid.NewString()
	if key != "" {
		if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", scope+":project-create:"+key); err != nil {
			return project.Resource{}, err
		}
		var prior string
		err := tx.QueryRow(ctx, "SELECT project_id,fingerprint FROM project_request_keys WHERE workspace_id=$1 AND key=$2", scope, key).Scan(&id, &prior)
		if err == nil {
			if prior != fingerprint {
				return project.Resource{}, r.Conflict
			}
			return Load(ctx, tx, scope, id, false)
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return project.Resource{}, err
		}
	}
	if _, err := tx.Exec(ctx, "INSERT INTO projects(id,workspace_id,project_type,content_type,name,metadata) VALUES($1,$2,$3,$3,$4,$5)", id, scope, input["content_type"], input["title"], body); err != nil {
		return project.Resource{}, err
	}
	if r.CreateInitialDocument == nil {
		return project.Resource{}, errors.New("content document writer unavailable")
	}
	if err := r.CreateInitialDocument(ctx, tx, doc, scope, id, text(input["content_type"]), text(input["title"])); err != nil {
		return project.Resource{}, err
	}
	if _, err := tx.Exec(ctx, "INSERT INTO project_projections(id,workspace_id,document_id,input) VALUES($1,$2,$3,$4)", id, scope, doc, body); err != nil {
		return project.Resource{}, err
	}
	brief, _ := json.Marshal(input["brief"])
	if _, err := tx.Exec(ctx, "INSERT INTO project_brief_versions(workspace_id,project_id,version,payload) VALUES($1,$2,1,$3)", scope, id, brief); err != nil {
		return project.Resource{}, err
	}
	if key != "" {
		if _, err := tx.Exec(ctx, "INSERT INTO project_request_keys(workspace_id,key,fingerprint,project_id) VALUES($1,$2,$3,$4)", scope, key, fingerprint, id); err != nil {
			return project.Resource{}, err
		}
	}
	if r.Audit != nil {
		if err := r.Audit(ctx, tx, scope, "project.created", id); err != nil {
			return project.Resource{}, err
		}
	}
	return Load(ctx, tx, scope, id, false)
}
