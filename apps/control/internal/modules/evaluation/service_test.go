package evaluation

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"
)

type policyRepositoryStub struct {
	created   *EvaluationPolicyVersion
	createErr error
}

func (r *policyRepositoryStub) Benchmark(context.Context, string, string) (json.RawMessage, error) {
	return nil, nil
}

func (r *policyRepositoryStub) Benchmarks(context.Context, string) ([]json.RawMessage, error) {
	return nil, nil
}
func (r *policyRepositoryStub) BenchmarksFiltered(context.Context, string, BenchmarkFilters) ([]json.RawMessage, error) {
	return nil, nil
}

func (r *policyRepositoryStub) CreatePolicy(_ context.Context, _, _ string, policy *EvaluationPolicyVersion) error {
	r.created = policy
	return r.createErr
}

func (*policyRepositoryStub) PublishPolicy(context.Context, string, string, int) error { return nil }
func (*policyRepositoryStub) ArchivePolicy(context.Context, string, string, int) error { return nil }
func (*policyRepositoryStub) GetPolicy(context.Context, string, string, int) (*EvaluationPolicyVersion, error) {
	return nil, nil
}
func (*policyRepositoryStub) ListPolicies(context.Context, string, PolicyFilters) ([]*EvaluationPolicyVersion, error) {
	return nil, nil
}
func (*policyRepositoryStub) GetPublishedPolicy(context.Context, string, string) (*EvaluationPolicyVersion, error) {
	return nil, nil
}
func (*policyRepositoryStub) BenchmarkReport(context.Context, string, string) (*EvaluationReport, error) {
	return nil, ErrNotFound
}
func (*policyRepositoryStub) GetWorkspaceStats(context.Context, string, time.Time) (*WorkspaceStats, error) {
	return &WorkspaceStats{}, nil
}
func (*policyRepositoryStub) GetDimensionTrends(context.Context, string, string, int) ([]*TrendPoint, error) {
	return []*TrendPoint{}, nil
}
func (*policyRepositoryStub) GetTopIssues(context.Context, string, int) ([]*IssueFrequency, error) {
	return []*IssueFrequency{}, nil
}
func (*policyRepositoryStub) ListDimensions(context.Context, string) ([]*DimensionDefinition, error) {
	return []*DimensionDefinition{}, nil
}
func (*policyRepositoryStub) RegisterDimension(context.Context, *DimensionDefinition) error {
	return nil
}

func TestCreatePolicyStoresOnlyValidatedDrafts(t *testing.T) {
	repository := &policyRepositoryStub{}
	service := NewService(repository)
	policy := validPolicy()
	policy.Status = ""
	if err := service.CreatePolicy(context.Background(), "workspace-1", policy.PolicyID, &policy); err != nil {
		t.Fatalf("create valid policy: %v", err)
	}
	if repository.created == nil || repository.created.Status != PolicyStatusDraft {
		t.Fatal("new policy was not stored as a draft")
	}

	invalid := validPolicy()
	invalid.Status = PolicyStatusPublished
	if err := service.CreatePolicy(context.Background(), "workspace-1", invalid.PolicyID, &invalid); !errors.Is(err, ErrInvalid) {
		t.Fatalf("published policy creation error = %v, want ErrInvalid", err)
	}
	if repository.created == nil || repository.created.Status != PolicyStatusDraft {
		t.Fatal("invalid policy reached the repository")
	}
}

func TestListBenchmarksRejectsUnwritableRunningStatus(t *testing.T) {
	service := NewService(&policyRepositoryStub{})
	_, err := service.ListBenchmarks(context.Background(), "workspace-1", BenchmarkFilters{Status: "running"})
	if !errors.Is(err, ErrInvalid) {
		t.Fatalf("running filter error = %v, want ErrInvalid", err)
	}
}
