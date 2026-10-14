package resources

import (
	"context"
	taskmodule "github.com/sakuya/novel-agent/apps/control/internal/modules/task"
)

type taskSubmitter struct {
	api     *ResourceAPI
	project ProjectResource
}

func (s taskSubmitter) Submit(ctx context.Context, scope string, request taskmodule.Request) (taskmodule.Receipt, error) {
	command, err := s.api.submitOperation(ctx, scope, s.project, request.Operation, request.Arguments, request.IdempotencyKey)
	return taskmodule.Receipt{JobID: command.JobID, CommandID: command.ID, ProjectID: request.ProjectID, Operation: request.Operation, Status: "queued"}, err
}
