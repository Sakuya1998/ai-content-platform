package postgres

import (
	"context"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/task"
	"strings"
)

type CandidateStatusReader func(context.Context, pgx.Tx, string, string, string) (bool, error)

func OperationValidator(readCandidateStatus CandidateStatusReader) func(context.Context, pgx.Tx, string, project.Resource, string, map[string]any) error {
	return func(ctx context.Context, tx pgx.Tx, scope string, novel project.Resource, operation string, args map[string]any) error {
		if err := validateOperationArguments(operation, args); err != nil {
			return err
		}
		candidate := text(args["candidate_id"])
		if candidate == "" {
			return nil
		}
		if readCandidateStatus == nil {
			return errors.New("candidate status reader unavailable")
		}
		stale, err := readCandidateStatus(ctx, tx, scope, novel.ID, candidate)
		if err != nil {
			return err
		}
		if stale {
			return task.ErrConflict
		}
		return nil
	}
}

func validateOperationArguments(operation string, args map[string]any) error {
	if operation == "candidates" {
		if args["count"] == nil {
			args["count"] = 3
		}
		if n := task.Integer(args["count"]); n < 2 || n > 4 {
			return task.ErrInvalid
		}
	}
	if operation == "memory_evaluate" || operation == "memory_rebuild" {
		if args["k"] == nil {
			args["k"] = 5
		}
		if k := task.Integer(args["k"]); k < 1 || k > 20 {
			return task.ErrInvalid
		}
	}
	if operation == "revision" && (task.Integer(args["block_index"]) < 1 || text(args["feedback"]) == "") {
		return task.ErrInvalid
	}
	if operation == "canon" {
		actions := []string{"upsert_fact", "deprecate_fact", "confirm_fact", "merge_alias", "update_character", "upsert_thread", "update_thread_status", "upsert_thread_beat"}
		found := false
		for _, action := range actions {
			if args["action"] == action {
				found = true
			}
		}
		if !found || strings.TrimSpace(text(args["reason"])) == "" {
			return task.ErrInvalid
		}
	}
	return nil
}
