package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/evaluation"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/database"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgtype"
)

const policyColumns = `policy_id,version,engine_version,gate_threshold,critical_threshold,rules,status,published_at,created_at`

func (r *Repository) CreatePolicy(ctx context.Context, workspace, policyID string, policy *evaluation.EvaluationPolicyVersion) error {
	if policy == nil || policy.PolicyID != policyID || policy.Status != evaluation.PolicyStatusDraft || policy.PublishedAt != nil {
		return evaluation.ErrInvalid
	}
	if err := policy.Validate(); err != nil {
		return fmt.Errorf("%w: %v", evaluation.ErrInvalid, err)
	}
	rules, err := json.Marshal(policy.Rules)
	if err != nil {
		return err
	}
	return r.withinWrite(ctx, workspace, func(tx pgx.Tx) error {
		if err := tx.QueryRow(ctx, `INSERT INTO evaluation_policies
			(workspace_id,policy_id,version,engine_version,gate_threshold,critical_threshold,rules,status)
			VALUES($1,$2,$3,$4,$5,$6,$7,'draft') RETURNING created_at`,
			workspace, policyID, policy.Version, policy.EngineVersion, policy.GateThreshold, policy.CriticalThreshold, rules,
		).Scan(&policy.CreatedAt); err != nil {
			return err
		}
		return r.writeAudit(ctx, tx, workspace, "evaluation_policy.created", policyID, policy.Version)
	})
}

func (r *Repository) PublishPolicy(ctx context.Context, workspace, policyID string, version int) error {
	return r.withinWrite(ctx, workspace, func(tx pgx.Tx) error {
		var status string
		if err := tx.QueryRow(ctx, `SELECT status FROM evaluation_policies
			WHERE workspace_id=$1 AND policy_id=$2 AND version=$3 FOR UPDATE`, workspace, policyID, version).Scan(&status); err != nil {
			return err
		}
		if status != evaluation.PolicyStatusDraft {
			return evaluation.ErrConflict
		}
		rows, err := tx.Query(ctx, `UPDATE evaluation_policies SET status='archived'
			WHERE workspace_id=$1 AND policy_id=$2 AND status='published' RETURNING version`, workspace, policyID)
		if err != nil {
			return err
		}
		archived := []int{}
		for rows.Next() {
			var previous int
			if err = rows.Scan(&previous); err != nil {
				rows.Close()
				return err
			}
			archived = append(archived, previous)
		}
		if err = rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		if _, err = tx.Exec(ctx, `UPDATE evaluation_policies SET status='published',published_at=now()
			WHERE workspace_id=$1 AND policy_id=$2 AND version=$3`, workspace, policyID, version); err != nil {
			return err
		}
		for _, previous := range archived {
			if err = r.writeAudit(ctx, tx, workspace, "evaluation_policy.archived", policyID, previous); err != nil {
				return err
			}
		}
		return r.writeAudit(ctx, tx, workspace, "evaluation_policy.published", policyID, version)
	})
}

func (r *Repository) ArchivePolicy(ctx context.Context, workspace, policyID string, version int) error {
	return r.withinWrite(ctx, workspace, func(tx pgx.Tx) error {
		var status string
		if err := tx.QueryRow(ctx, `SELECT status FROM evaluation_policies
			WHERE workspace_id=$1 AND policy_id=$2 AND version=$3 FOR UPDATE`, workspace, policyID, version).Scan(&status); err != nil {
			return err
		}
		if status != evaluation.PolicyStatusPublished {
			return evaluation.ErrConflict
		}
		if _, err := tx.Exec(ctx, `UPDATE evaluation_policies SET status='archived'
			WHERE workspace_id=$1 AND policy_id=$2 AND version=$3`, workspace, policyID, version); err != nil {
			return err
		}
		return r.writeAudit(ctx, tx, workspace, "evaluation_policy.archived", policyID, version)
	})
}

func (r *Repository) GetPolicy(ctx context.Context, workspace, policyID string, version int) (*evaluation.EvaluationPolicyVersion, error) {
	var policy *evaluation.EvaluationPolicyVersion
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		var err error
		policy, err = scanPolicy(tx.QueryRow(ctx, `SELECT `+policyColumns+` FROM evaluation_policies
			WHERE workspace_id=$1 AND policy_id=$2 AND version=$3`, workspace, policyID, version))
		return err
	})
	return policy, policyError(err)
}

func (r *Repository) ListPolicies(ctx context.Context, workspace string, filters evaluation.PolicyFilters) ([]*evaluation.EvaluationPolicyVersion, error) {
	items := []*evaluation.EvaluationPolicyVersion{}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `SELECT `+policyColumns+` FROM evaluation_policies
			WHERE workspace_id=$1 AND ($2='' OR policy_id=$2) AND ($3='' OR status=$3)
			ORDER BY policy_id,version DESC`, workspace, filters.PolicyID, filters.Status)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			policy, scanErr := scanPolicy(rows)
			if scanErr != nil {
				return scanErr
			}
			items = append(items, policy)
		}
		return rows.Err()
	})
	return items, policyError(err)
}

func (r *Repository) GetPublishedPolicy(ctx context.Context, workspace, policyID string) (*evaluation.EvaluationPolicyVersion, error) {
	var policy *evaluation.EvaluationPolicyVersion
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		var err error
		policy, err = scanPolicy(tx.QueryRow(ctx, `SELECT `+policyColumns+` FROM evaluation_policies
			WHERE workspace_id=$1 AND policy_id=$2 AND status='published'`, workspace, policyID))
		return err
	})
	return policy, policyError(err)
}

type policyRow interface {
	Scan(...any) error
}

func scanPolicy(row policyRow) (*evaluation.EvaluationPolicyVersion, error) {
	var policy evaluation.EvaluationPolicyVersion
	var rules []byte
	var publishedAt pgtype.Timestamptz
	if err := row.Scan(&policy.PolicyID, &policy.Version, &policy.EngineVersion, &policy.GateThreshold,
		&policy.CriticalThreshold, &rules, &policy.Status, &publishedAt, &policy.CreatedAt); err != nil {
		return nil, err
	}
	if err := json.Unmarshal(rules, &policy.Rules); err != nil {
		return nil, err
	}
	if publishedAt.Valid {
		policy.PublishedAt = &publishedAt.Time
	}
	return &policy, nil
}

func (r *Repository) withinWrite(ctx context.Context, workspace string, fn func(pgx.Tx) error) error {
	if r.pool == nil {
		return evaluation.ErrUnavailable
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.pool, workspace)
	if err != nil {
		return policyError(err)
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if err = fn(tx); err != nil {
		return policyError(err)
	}
	return policyError(tx.Commit(ctx))
}

func (r *Repository) writeAudit(ctx context.Context, tx pgx.Tx, workspace, action, policyID string, version int) error {
	if r.audit == nil {
		return errors.New("evaluation audit writer unavailable")
	}
	principal, ok := identity.FromContext(ctx)
	if !ok || principal.WorkspaceID != workspace {
		return identity.ErrUnauthorized
	}
	return r.audit(ctx, tx, principal, action, "evaluation_policy", fmt.Sprintf("%s@%d", policyID, version))
}

func policyError(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return evaluation.ErrNotFound
	}
	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) {
		switch pgErr.Code {
		case "23505":
			return fmt.Errorf("%w: evaluation policy version already exists or another version is published", evaluation.ErrConflict)
		case "23503", "23514":
			return fmt.Errorf("%w: evaluation policy violates a database constraint", evaluation.ErrInvalid)
		}
	}
	return err
}
