package app

import (
	"errors"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/project"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/task"
)

func normalizeResourceError(err error) error {
	var databaseError *pgconn.PgError
	if errors.Is(err, pgx.ErrNoRows) {
		return project.ErrNotFound
	}
	if errors.As(err, &databaseError) && databaseError.Code == "23505" {
		return task.ErrConflict
	}
	return err
}
