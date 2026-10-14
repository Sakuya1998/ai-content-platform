package observability

import (
	"context"
	"encoding/json"
	"errors"
)

type Repository interface {
	Latest(context.Context, string, string, string, int) (json.RawMessage, error)
	Summary(context.Context, string) (map[string]any, error)
	Ready(context.Context) error
}

type Service struct{ repository Repository }

func NewService(repository Repository) *Service { return &Service{repository: repository} }

func (s *Service) Latest(ctx context.Context, workspace, project, kind string, limit int) (json.RawMessage, error) {
	if limit < 1 || limit > 500 {
		return nil, errors.New("invalid projection limit")
	}
	return s.repository.Latest(ctx, workspace, project, kind, limit)
}

func (s *Service) Summary(ctx context.Context, workspace string) (map[string]any, error) {
	return s.repository.Summary(ctx, workspace)
}

func (s *Service) Ready(ctx context.Context) error { return s.repository.Ready(ctx) }
