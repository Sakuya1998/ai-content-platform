package evaluation

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

type Repository interface {
	Benchmark(context.Context, string, string) (json.RawMessage, error)
	Benchmarks(context.Context, string) ([]json.RawMessage, error)
	BenchmarksFiltered(context.Context, string, BenchmarkFilters) ([]json.RawMessage, error)
	CreatePolicy(context.Context, string, string, *EvaluationPolicyVersion) error
	PublishPolicy(context.Context, string, string, int) error
	ArchivePolicy(context.Context, string, string, int) error
	GetPolicy(context.Context, string, string, int) (*EvaluationPolicyVersion, error)
	ListPolicies(context.Context, string, PolicyFilters) ([]*EvaluationPolicyVersion, error)
	GetPublishedPolicy(context.Context, string, string) (*EvaluationPolicyVersion, error)
	BenchmarkReport(context.Context, string, string) (*EvaluationReport, error)
	GetWorkspaceStats(context.Context, string, time.Time) (*WorkspaceStats, error)
	GetDimensionTrends(context.Context, string, string, int) ([]*TrendPoint, error)
	GetTopIssues(context.Context, string, int) ([]*IssueFrequency, error)
	ListDimensions(context.Context, string) ([]*DimensionDefinition, error)
	RegisterDimension(context.Context, *DimensionDefinition) error
}

type Service struct{ repository Repository }

func NewService(repository Repository) *Service { return &Service{repository: repository} }

func (s *Service) Benchmark(ctx context.Context, workspace, id string) (json.RawMessage, error) {
	return s.repository.Benchmark(ctx, workspace, id)
}

func (s *Service) Benchmarks(ctx context.Context, workspace string) ([]json.RawMessage, error) {
	return s.repository.Benchmarks(ctx, workspace)
}

func (s *Service) ListBenchmarks(ctx context.Context, workspace string, filters BenchmarkFilters) ([]json.RawMessage, error) {
	if s.repository == nil {
		return nil, ErrUnavailable
	}
	if strings.TrimSpace(workspace) == "" || filters.Status != "" && filters.Status != "completed" && filters.Status != "passed" && filters.Status != "failed" {
		return nil, ErrInvalid
	}
	filters.JobID = strings.TrimSpace(filters.JobID)
	return s.repository.BenchmarksFiltered(ctx, workspace, filters)
}

func (s *Service) CreatePolicy(ctx context.Context, workspace, policyID string, policy *EvaluationPolicyVersion) error {
	policyID = strings.TrimSpace(policyID)
	if s.repository == nil || strings.TrimSpace(workspace) == "" || policy == nil || policyID == "" {
		return ErrInvalid
	}
	if policy.PolicyID != "" && strings.TrimSpace(policy.PolicyID) != policyID {
		return fmt.Errorf("%w: policy_id does not match request", ErrInvalid)
	}
	if policy.Status != "" && policy.Status != PolicyStatusDraft || policy.PublishedAt != nil {
		return fmt.Errorf("%w: new policies must be drafts", ErrInvalid)
	}
	validated := *policy
	validated.PolicyID = policyID
	validated.Status = PolicyStatusDraft
	validated.PublishedAt = nil
	validated.CreatedAt = validated.CreatedAt.UTC()
	if err := validated.Validate(); err != nil {
		return fmt.Errorf("%w: %v", ErrInvalid, err)
	}
	return s.repository.CreatePolicy(ctx, workspace, policyID, &validated)
}

func (s *Service) PublishPolicy(ctx context.Context, workspace, policyID string, version int) error {
	if s.repository == nil || strings.TrimSpace(workspace) == "" || strings.TrimSpace(policyID) == "" || version < 1 {
		return ErrInvalid
	}
	return s.repository.PublishPolicy(ctx, workspace, strings.TrimSpace(policyID), version)
}

func (s *Service) ArchivePolicy(ctx context.Context, workspace, policyID string, version int) error {
	if s.repository == nil || strings.TrimSpace(workspace) == "" || strings.TrimSpace(policyID) == "" || version < 1 {
		return ErrInvalid
	}
	return s.repository.ArchivePolicy(ctx, workspace, strings.TrimSpace(policyID), version)
}

func (s *Service) GetPolicy(ctx context.Context, workspace, policyID string, version int) (*EvaluationPolicyVersion, error) {
	if s.repository == nil {
		return nil, ErrUnavailable
	}
	if strings.TrimSpace(workspace) == "" || strings.TrimSpace(policyID) == "" || version < 1 {
		return nil, ErrInvalid
	}
	return s.repository.GetPolicy(ctx, workspace, strings.TrimSpace(policyID), version)
}

func (s *Service) ListPolicies(ctx context.Context, workspace string, filters PolicyFilters) ([]*EvaluationPolicyVersion, error) {
	if s.repository == nil {
		return nil, ErrUnavailable
	}
	if strings.TrimSpace(workspace) == "" {
		return nil, ErrInvalid
	}
	filters.PolicyID = strings.TrimSpace(filters.PolicyID)
	if filters.Status != "" && filters.Status != PolicyStatusDraft && filters.Status != PolicyStatusPublished && filters.Status != PolicyStatusArchived {
		return nil, ErrInvalid
	}
	return s.repository.ListPolicies(ctx, workspace, filters)
}

func (s *Service) GetPublishedPolicy(ctx context.Context, workspace, policyID string) (*EvaluationPolicyVersion, error) {
	if s.repository == nil {
		return nil, ErrUnavailable
	}
	if strings.TrimSpace(workspace) == "" || strings.TrimSpace(policyID) == "" {
		return nil, ErrInvalid
	}
	return s.repository.GetPublishedPolicy(ctx, workspace, strings.TrimSpace(policyID))
}

func (s *Service) CompareBenchmarks(ctx context.Context, workspace, baselineID, currentID string, threshold float64) (*EvaluationComparison, error) {
	if s.repository == nil {
		return nil, ErrUnavailable
	}
	if strings.TrimSpace(workspace) == "" || strings.TrimSpace(baselineID) == "" || strings.TrimSpace(currentID) == "" || !validScore(threshold) {
		return nil, ErrInvalid
	}
	baseline, err := s.repository.BenchmarkReport(ctx, workspace, baselineID)
	if err != nil {
		return nil, err
	}
	current, err := s.repository.BenchmarkReport(ctx, workspace, currentID)
	if err != nil {
		return nil, err
	}
	return CompareReports(baseline, current, threshold), nil
}

func (s *Service) GetWorkspaceStats(ctx context.Context, workspace string, since time.Time) (*WorkspaceStats, error) {
	if s.repository == nil {
		return nil, ErrUnavailable
	}
	if strings.TrimSpace(workspace) == "" {
		return nil, ErrInvalid
	}
	return s.repository.GetWorkspaceStats(ctx, workspace, since)
}

func (s *Service) GetDimensionTrends(ctx context.Context, workspace, dimension string, limit int) ([]*TrendPoint, error) {
	if s.repository == nil {
		return nil, ErrUnavailable
	}
	if strings.TrimSpace(workspace) == "" || strings.TrimSpace(dimension) == "" || limit < 1 || limit > 500 {
		return nil, ErrInvalid
	}
	return s.repository.GetDimensionTrends(ctx, workspace, strings.TrimSpace(dimension), limit)
}

func (s *Service) GetTopIssues(ctx context.Context, workspace string, limit int) ([]*IssueFrequency, error) {
	if s.repository == nil {
		return nil, ErrUnavailable
	}
	if strings.TrimSpace(workspace) == "" || limit < 1 || limit > 100 {
		return nil, ErrInvalid
	}
	return s.repository.GetTopIssues(ctx, workspace, limit)
}

func (s *Service) ListDimensions(ctx context.Context, category string) ([]*DimensionDefinition, error) {
	if s.repository == nil {
		return nil, ErrUnavailable
	}
	category = strings.TrimSpace(category)
	if category != "" && category != "structure" && category != "content" && category != "style" {
		return nil, ErrInvalid
	}
	return s.repository.ListDimensions(ctx, category)
}

func (s *Service) RegisterDimension(ctx context.Context, dimension *DimensionDefinition) error {
	if s.repository == nil {
		return ErrUnavailable
	}
	if dimension == nil || strings.TrimSpace(dimension.Key) == "" || strings.TrimSpace(dimension.DisplayName) == "" ||
		(dimension.Category != "structure" && dimension.Category != "content" && dimension.Category != "style") || len(dimension.CheckKeys) == 0 {
		return ErrInvalid
	}
	return s.repository.RegisterDimension(ctx, dimension)
}
