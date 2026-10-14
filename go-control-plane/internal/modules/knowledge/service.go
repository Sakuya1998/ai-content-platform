package knowledge

import (
	"context"
	"encoding/json"
)

type Repository interface {
	List(context.Context, string, string, string) ([]json.RawMessage, error)
}
type Service struct{ repository Repository }

func NewService(repository Repository) *Service { return &Service{repository: repository} }
func (s *Service) List(ctx context.Context, workspace, project, kind string) ([]json.RawMessage, error) {
	return s.repository.List(ctx, workspace, project, kind)
}
