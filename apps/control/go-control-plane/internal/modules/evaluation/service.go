package evaluation

import (
	"context"
	"encoding/json"
)

type Repository interface {
	Benchmark(context.Context, string, string) (json.RawMessage, error)
	Benchmarks(context.Context, string) ([]json.RawMessage, error)
}

type Service struct{ repository Repository }

func NewService(repository Repository) *Service { return &Service{repository: repository} }

func (s *Service) Benchmark(ctx context.Context, workspace, id string) (json.RawMessage, error) {
	return s.repository.Benchmark(ctx, workspace, id)
}

func (s *Service) Benchmarks(ctx context.Context, workspace string) ([]json.RawMessage, error) {
	return s.repository.Benchmarks(ctx, workspace)
}
