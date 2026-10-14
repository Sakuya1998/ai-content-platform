package postgres

import (
	"context"
	"encoding/json"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/sakuya/novel-agent/apps/control/internal/contracts"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/task"
)

type Job = task.Job

func (a Adapter) List(ctx context.Context, scope, project string, limit int) ([]map[string]any, error) {
	values := []map[string]any{}
	err := a.within(ctx, scope, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `SELECT id,project_id,status,last_event_sequence,request,result,error,created_at,updated_at FROM jobs WHERE workspace_id=$1 AND ($2='' OR project_id::text=$2) ORDER BY created_at DESC LIMIT $3`, scope, project, limit)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var id, project, status string
			var sequence int64
			var request, result, failure []byte
			var created, updated time.Time
			if err = rows.Scan(&id, &project, &status, &sequence, &request, &result, &failure, &created, &updated); err != nil {
				return err
			}
			var command contracts.Command
			if err = json.Unmarshal(request, &command); err != nil {
				return err
			}
			values = append(values, map[string]any{"job_id": id, "project_id": project, "status": status, "sequence": sequence, "request": json.RawMessage(command.Payload), "result": json.RawMessage(result), "error": json.RawMessage(failure), "created_at": created, "updated_at": updated})
		}
		return rows.Err()
	})
	return values, err
}
func (a Adapter) Get(ctx context.Context, scope, id string) (Job, error) {
	var job Job
	err := a.within(ctx, scope, func(tx pgx.Tx) error {
		var body []byte
		if err := tx.QueryRow(ctx, "SELECT id,status,last_event_sequence,result,request,error FROM jobs WHERE workspace_id=$1 AND id=$2", scope, id).Scan(&job.ID, &job.Status, &job.Sequence, &job.Result, &body, &job.Failure); err != nil {
			return err
		}
		return json.Unmarshal(body, &job.Command)
	})
	return job, err
}
func (a Adapter) Control(ctx context.Context, scope string, command contracts.Command) error {
	return a.within(ctx, scope, func(tx pgx.Tx) error {
		if err := a.Outbox.SubmitWithin(ctx, tx, command); err != nil {
			return err
		}
		if a.Audit != nil {
			return a.Audit(ctx, tx, scope, "project.job."+command.Type, command.ProjectID)
		}
		return nil
	})
}
func (a Adapter) EventSequence(ctx context.Context, scope, job, event string) (int64, error) {
	var sequence int64
	err := a.within(ctx, scope, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT sequence FROM runtime_event_inbox WHERE workspace_id=$1 AND job_id=$2 AND event_id=$3 AND applied_at IS NOT NULL", scope, job, event).Scan(&sequence)
	})
	return sequence, err
}
