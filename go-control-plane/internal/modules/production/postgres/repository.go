package postgres

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/go-control-plane/internal/contracts"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/content"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/execution"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/governance"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/database"
)

type CommandPort interface {
	GenerateWithSnapshot(context.Context, string, execution.Request, map[string]any, pgx.Tx) (contracts.Command, error)
	SubmitWithin(context.Context, pgx.Tx, contracts.Command) error
}

type FrozenModelProfileReader = func(context.Context, pgx.Tx, string, string) (governance.ModelProfile, error)
type ContentVersionReader = func(context.Context, pgx.Tx, string, string, string) (content.ContentVersion, error)
type ContentVersionIDByJobReader = func(context.Context, pgx.Tx, string, string) (string, error)

type RepositorySettings struct {
	SnapshotFreezer      execution.SnapshotFreezer
	FrozenModelProfile   FrozenModelProfileReader
	ContentVersion       ContentVersionReader
	VersionIDBySourceJob ContentVersionIDByJobReader
}

type Repository struct {
	pool                 *pgxpool.Pool
	commands             CommandPort
	snapshotFreezer      execution.SnapshotFreezer
	frozenModelProfile   FrozenModelProfileReader
	contentVersion       ContentVersionReader
	versionIDBySourceJob ContentVersionIDByJobReader
}

func NewRepository(pool *pgxpool.Pool, commands CommandPort, settings RepositorySettings) *Repository {
	return &Repository{
		pool: pool, commands: commands, snapshotFreezer: settings.SnapshotFreezer,
		frozenModelProfile: settings.FrozenModelProfile, contentVersion: settings.ContentVersion,
		versionIDBySourceJob: settings.VersionIDBySourceJob,
	}
}

func (r *Repository) WithSnapshotFreezer(freezer execution.SnapshotFreezer) *Repository {
	r.snapshotFreezer = freezer
	return r
}

func (r *Repository) WithFrozenModelProfileReader(reader FrozenModelProfileReader) *Repository {
	r.frozenModelProfile = reader
	return r
}

func (r *Repository) WithContentVersionReader(reader ContentVersionReader) *Repository {
	r.contentVersion = reader
	return r
}

func (r *Repository) within(ctx context.Context, workspace string, fn func(pgx.Tx) error) error {
	if r.pool == nil {
		return errors.New("production database unavailable")
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.pool, workspace)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (r *Repository) workspaces(ctx context.Context) ([]string, error) {
	if r.pool == nil {
		return nil, errors.New("production database unavailable")
	}
	rows, err := r.pool.Query(ctx, "SELECT id FROM workspaces ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	workspaces := []string{}
	for rows.Next() {
		var workspace string
		if err = rows.Scan(&workspace); err != nil {
			return nil, err
		}
		workspaces = append(workspaces, workspace)
	}
	return workspaces, rows.Err()
}
