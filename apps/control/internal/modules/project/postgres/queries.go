package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/jackc/pgx/v5"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/task"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/database"
)

func (r Repository) Within(ctx context.Context, scope string, fn func(pgx.Tx) error) error {
	if r.Pool == nil {
		return project.ErrNotFound
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.Pool, scope)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (r Repository) audit(ctx context.Context, tx pgx.Tx, scope, action, id string) error {
	if r.Audit == nil {
		return errors.New("audit writer unavailable")
	}
	return r.Audit(ctx, tx, scope, action, id)
}

func EnsureIdle(ctx context.Context, tx pgx.Tx, scope, id string) error {
	var active bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM jobs WHERE workspace_id=$1 AND project_id=$2 AND status IN ('queued','running','cancel_requested'))", scope, id).Scan(&active); err != nil {
		return err
	}
	if active {
		return task.ErrConflict
	}
	return nil
}

func (r Repository) Get(ctx context.Context, scope, id string) (project.Resource, error) {
	var resource project.Resource
	err := r.Within(ctx, scope, func(tx pgx.Tx) error { var err error; resource, err = Load(ctx, tx, scope, id, false); return err })
	return resource, err
}

func (r Repository) List(ctx context.Context, scope, cursor string, limit int) ([]project.Resource, error) {
	values := []project.Resource{}
	err := r.Within(ctx, scope, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, "SELECT id FROM project_projections WHERE workspace_id=$1 AND ($2='' OR id::text>$2) ORDER BY id LIMIT $3", scope, cursor, limit)
		if err != nil {
			return err
		}
		ids := []string{}
		for rows.Next() {
			var id string
			if err = rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			ids = append(ids, id)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for _, id := range ids {
			resource, err := Load(ctx, tx, scope, id, false)
			if err != nil {
				return err
			}
			values = append(values, resource)
		}
		return nil
	})
	return values, err
}

func (r Repository) Delete(ctx context.Context, scope, id string) error {
	return r.Within(ctx, scope, func(tx pgx.Tx) error {
		if _, err := Load(ctx, tx, scope, id, true); err != nil {
			return err
		}
		if err := EnsureIdle(ctx, tx, scope, id); err != nil {
			return err
		}
		if err := r.audit(ctx, tx, scope, "project.deleted", id); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, "DELETE FROM projects WHERE id=$1 AND workspace_id=$2", id, scope)
		return err
	})
}

func (r Repository) Update(ctx context.Context, scope, id string, expected int64, input map[string]any) (project.Resource, error) {
	var resource project.Resource
	err := r.Within(ctx, scope, func(tx pgx.Tx) error {
		var err error
		resource, err = Load(ctx, tx, scope, id, true)
		if err != nil {
			return err
		}
		if expected != resource.Revision {
			return task.ErrConflict
		}
		if err = EnsureIdle(ctx, tx, scope, id); err != nil {
			return err
		}
		body, _ := json.Marshal(input)
		if _, err = tx.Exec(ctx, "UPDATE projects SET name=$1,metadata=$2,updated_at=now() WHERE workspace_id=$3 AND id=$4", input["title"], body, scope, id); err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, "UPDATE project_projections SET input=$1,revision=revision+1,updated_at=now() WHERE workspace_id=$2 AND id=$3", body, scope, id); err != nil {
			return err
		}
		resource, err = Load(ctx, tx, scope, id, false)
		return err
	})
	return resource, err
}

func (r Repository) UpdateBrief(ctx context.Context, scope, id string, expected int, etag string, brief map[string]any, summary string) (project.Resource, bool, int64, error) {
	var resource project.Resource
	changed := false
	var stale int64
	err := r.Within(ctx, scope, func(tx pgx.Tx) error {
		var err error
		resource, err = Load(ctx, tx, scope, id, true)
		if err != nil {
			return err
		}
		if err = EnsureIdle(ctx, tx, scope, id); err != nil {
			return err
		}
		if etag != "" && etag != fmt.Sprintf("\"%d\"", resource.Revision) || expected > 0 && expected != resource.BriefVersion {
			return task.ErrConflict
		}
		body, _ := json.Marshal(brief)
		prior, _ := json.Marshal(resource.Input["brief"])
		if string(body) == string(prior) {
			return nil
		}
		changed = true
		resource.BriefVersion++
		resource.Input["brief"] = brief
		if _, err = tx.Exec(ctx, "INSERT INTO project_brief_versions(workspace_id,project_id,version,payload,change_summary) VALUES($1,$2,$3,$4,$5)", scope, id, resource.BriefVersion, body, summary); err != nil {
			return err
		}
		definition, _ := json.Marshal(resource.Input)
		if _, err = tx.Exec(ctx, "UPDATE project_projections SET input=$1,brief_version=$2,revision=revision+1,updated_at=now() WHERE id=$3 AND workspace_id=$4", definition, resource.BriefVersion, id, scope); err != nil {
			return err
		}
		if r.MarkCandidatesStale == nil {
			return errors.New("candidate invalidator unavailable")
		}
		stale, err = r.MarkCandidatesStale(ctx, tx, scope, id)
		if err != nil {
			return err
		}
		if err = r.audit(ctx, tx, scope, "project.brief.updated", id); err != nil {
			return err
		}
		resource, err = Load(ctx, tx, scope, id, false)
		return err
	})
	return resource, changed, stale, err
}

func (r Repository) BriefVersions(ctx context.Context, scope, id string) ([]map[string]any, error) {
	values := []map[string]any{}
	err := r.Within(ctx, scope, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, "SELECT version,payload,change_summary FROM project_brief_versions WHERE workspace_id=$1 AND project_id=$2 ORDER BY version DESC", scope, id)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var version int
			var body []byte
			var summary string
			if err = rows.Scan(&version, &body, &summary); err != nil {
				return err
			}
			var brief any
			if err = json.Unmarshal(body, &brief); err != nil {
				return err
			}
			values = append(values, map[string]any{"version": version, "brief": brief, "change_summary": summary})
		}
		return rows.Err()
	})
	return values, err
}
