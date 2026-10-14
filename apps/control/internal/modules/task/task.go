// Package task defines public task semantics independently of execution adapters.
package task

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/sakuya/ai-content-platform/apps/control/internal/contracts"
)

type Request struct {
	ProjectID      string
	Operation      string
	Arguments      map[string]any
	IdempotencyKey string
}

type Receipt struct {
	JobID     string `json:"job_id"`
	CommandID string `json:"command_id"`
	ProjectID string `json:"project_id"`
	Operation string `json:"operation"`
	Status    string `json:"status"`
}

type Job struct {
	ID       string
	Status   string
	Sequence int64
	Result   json.RawMessage
	Command  contracts.Command
	Failure  json.RawMessage
}

type Submitter interface {
	Submit(context.Context, string, Request) (Receipt, error)
}
type Service struct{ Submitter Submitter }

func (s Service) Submit(ctx context.Context, scope string, request Request) (Receipt, error) {
	if request.Arguments == nil {
		request.Arguments = map[string]any{}
	}
	if request.ProjectID == "" || len(request.IdempotencyKey) > 200 {
		return Receipt{}, ErrInvalid
	}
	if err := Validate(request.Operation, request.Arguments); err != nil {
		return Receipt{}, err
	}
	return s.Submitter.Submit(ctx, scope, request)
}

var ErrInvalid = errors.New("invalid task request")
var ErrConflict = errors.New("message identity or state conflict")

func Integer(value any) int {
	switch n := value.(type) {
	case int:
		return n
	case float64:
		if n == float64(int(n)) {
			return int(n)
		}
	case json.Number:
		if v, err := n.Int64(); err == nil {
			return int(v)
		}
	}
	return 0
}

func Validate(operation string, args map[string]any) error {
	switch operation {
	case "run", "resume", "evaluation", "canon":
	case "candidates":
		if args["count"] == nil {
			args["count"] = 3
		}
		if n := Integer(args["count"]); n < 2 || n > 4 {
			return ErrInvalid
		}
	case "memory_rebuild", "memory_evaluate", "memory_search":
		if args["k"] == nil {
			args["k"] = 5
		}
		if n := Integer(args["k"]); n < 1 || n > 20 {
			return ErrInvalid
		}
	case "revision":
		feedback, _ := args["feedback"].(string)
		if Integer(args["block_index"]) < 1 || feedback == "" {
			return ErrInvalid
		}
	default:
		return ErrInvalid
	}
	return nil
}
