package operations

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/evaluation"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/google/uuid"
)

type evaluationPolicyRepositoryStub struct {
	items      map[string]*evaluation.EvaluationPolicyVersion
	reports    map[string]*evaluation.EvaluationReport
	dimensions []*evaluation.DimensionDefinition
	benchmarks []json.RawMessage
	filters    evaluation.BenchmarkFilters
}

func newEvaluationPolicyRepositoryStub() *evaluationPolicyRepositoryStub {
	return &evaluationPolicyRepositoryStub{
		items:      make(map[string]*evaluation.EvaluationPolicyVersion),
		reports:    make(map[string]*evaluation.EvaluationReport),
		dimensions: []*evaluation.DimensionDefinition{{Key: "length", Category: "structure", DisplayName: "Length", CheckKeys: []string{"text.length@1"}, IsBuiltin: true}},
	}
}

func evaluationPolicyKey(workspace, policyID string, version int) string {
	return workspace + "/" + policyID + "/" + strconv.Itoa(version)
}

func (*evaluationPolicyRepositoryStub) Benchmark(context.Context, string, string) (json.RawMessage, error) {
	return nil, nil
}
func (*evaluationPolicyRepositoryStub) Benchmarks(context.Context, string) ([]json.RawMessage, error) {
	return nil, nil
}
func (r *evaluationPolicyRepositoryStub) BenchmarksFiltered(_ context.Context, _ string, filters evaluation.BenchmarkFilters) ([]json.RawMessage, error) {
	r.filters = filters
	return r.benchmarks, nil
}
func (r *evaluationPolicyRepositoryStub) CreatePolicy(_ context.Context, workspace, policyID string, policy *evaluation.EvaluationPolicyVersion) error {
	copy := *policy
	r.items[evaluationPolicyKey(workspace, policyID, policy.Version)] = &copy
	return nil
}
func (r *evaluationPolicyRepositoryStub) PublishPolicy(_ context.Context, workspace, policyID string, version int) error {
	item := r.items[evaluationPolicyKey(workspace, policyID, version)]
	if item == nil {
		return evaluation.ErrNotFound
	}
	item.Status = evaluation.PolicyStatusPublished
	now := time.Now()
	item.PublishedAt = &now
	return nil
}
func (r *evaluationPolicyRepositoryStub) ArchivePolicy(_ context.Context, workspace, policyID string, version int) error {
	item := r.items[evaluationPolicyKey(workspace, policyID, version)]
	if item == nil {
		return evaluation.ErrNotFound
	}
	item.Status = evaluation.PolicyStatusArchived
	return nil
}
func (r *evaluationPolicyRepositoryStub) GetPolicy(_ context.Context, workspace, policyID string, version int) (*evaluation.EvaluationPolicyVersion, error) {
	item := r.items[evaluationPolicyKey(workspace, policyID, version)]
	if item == nil {
		return nil, evaluation.ErrNotFound
	}
	copy := *item
	return &copy, nil
}
func (r *evaluationPolicyRepositoryStub) ListPolicies(_ context.Context, workspace string, filters evaluation.PolicyFilters) ([]*evaluation.EvaluationPolicyVersion, error) {
	items := []*evaluation.EvaluationPolicyVersion{}
	for key, item := range r.items {
		if !strings.HasPrefix(key, workspace+"/") || filters.PolicyID != "" && item.PolicyID != filters.PolicyID || filters.Status != "" && item.Status != filters.Status {
			continue
		}
		copy := *item
		items = append(items, &copy)
	}
	return items, nil
}
func (r *evaluationPolicyRepositoryStub) GetPublishedPolicy(_ context.Context, workspace, policyID string) (*evaluation.EvaluationPolicyVersion, error) {
	for key, item := range r.items {
		if strings.HasPrefix(key, workspace+"/") && item.PolicyID == policyID && item.Status == evaluation.PolicyStatusPublished {
			copy := *item
			return &copy, nil
		}
	}
	return nil, evaluation.ErrNotFound
}
func (r *evaluationPolicyRepositoryStub) BenchmarkReport(_ context.Context, workspace, id string) (*evaluation.EvaluationReport, error) {
	item := r.reports[workspace+"/"+id]
	if item == nil {
		return nil, evaluation.ErrNotFound
	}
	copy := *item
	return &copy, nil
}
func (*evaluationPolicyRepositoryStub) GetWorkspaceStats(context.Context, string, time.Time) (*evaluation.WorkspaceStats, error) {
	return &evaluation.WorkspaceStats{TotalReports: 4, ScoredReports: 3, AverageScore: 82.5, PassedReports: 2, FailedReports: 1, PassRate: 66.7}, nil
}
func (*evaluationPolicyRepositoryStub) GetDimensionTrends(_ context.Context, _ string, dimension string, _ int) ([]*evaluation.TrendPoint, error) {
	return []*evaluation.TrendPoint{{CreatedAt: time.Date(2026, 10, 19, 0, 0, 0, 0, time.UTC), Score: 82.5, Count: 1}}, nil
}
func (*evaluationPolicyRepositoryStub) GetTopIssues(context.Context, string, int) ([]*evaluation.IssueFrequency, error) {
	return []*evaluation.IssueFrequency{}, nil
}
func (r *evaluationPolicyRepositoryStub) ListDimensions(_ context.Context, category string) ([]*evaluation.DimensionDefinition, error) {
	items := []*evaluation.DimensionDefinition{}
	for _, item := range r.dimensions {
		if category == "" || item.Category == category {
			items = append(items, item)
		}
	}
	return items, nil
}
func (*evaluationPolicyRepositoryStub) RegisterDimension(context.Context, *evaluation.DimensionDefinition) error {
	return nil
}

func TestEvaluationPolicyHTTPLifecycleAndValidation(t *testing.T) {
	repository := newEvaluationPolicyRepositoryStub()
	api := NewOperationsAPI(nil, evaluation.NewService(repository), nil, nil, "")
	policyJSON := `{"policy_id":"content-quality","version":1,"engine_version":"evaluation@1","gate_threshold":75,"critical_threshold":60,"rules":[{"key":"text.length@1","dimension":"length","weight":1,"critical":false,"parameters":{}}]}`
	response := sendEvaluationPolicyRequest(api, http.MethodPost, "/api/v2/evaluations/policies", policyJSON)
	if response.Code != http.StatusCreated {
		t.Fatalf("create status = %d: %s", response.Code, response.Body.String())
	}
	var created evaluation.EvaluationPolicyVersion
	if err := json.Unmarshal(response.Body.Bytes(), &created); err != nil || created.Status != evaluation.PolicyStatusDraft {
		t.Fatalf("created policy = %#v, err=%v", created, err)
	}

	response = sendEvaluationPolicyRequest(api, http.MethodPost, "/api/v2/evaluations/policies/content-quality/v/1/publish", "")
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"status":"published"`) {
		t.Fatalf("publish response = %d: %s", response.Code, response.Body.String())
	}
	response = sendEvaluationPolicyRequest(api, http.MethodPost, "/api/v2/evaluations/policies/content-quality/v/1/archive", "")
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"status":"archived"`) {
		t.Fatalf("archive response = %d: %s", response.Code, response.Body.String())
	}

	response = sendEvaluationPolicyRequest(api, http.MethodPost, "/api/v2/evaluations/policies", `{"policy_id":"empty","version":1,"engine_version":"evaluation@1","gate_threshold":75,"critical_threshold":60,"rules":[]}`)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("invalid policy status = %d: %s", response.Code, response.Body.String())
	}
	largeBody := `{"policy_id":"large","version":1,"engine_version":"evaluation@1","gate_threshold":75,"critical_threshold":60,"rules":[{"key":"large","dimension":"large","weight":1,"parameters":{"blob":"` + strings.Repeat("x", 300000) + `"}}]}`
	response = sendEvaluationPolicyRequest(api, http.MethodPost, "/api/v2/evaluations/policies", largeBody)
	if response.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversized policy status = %d: %s", response.Code, response.Body.String())
	}
}

func TestBenchmarkComparisonHTTPEndpointUsesPathBaselineAndBodyCurrent(t *testing.T) {
	repository := newEvaluationPolicyRepositoryStub()
	baselineID, currentID := uuid.NewString(), uuid.NewString()
	repository.reports["workspace-1/"+baselineID] = &evaluation.EvaluationReport{
		ID: baselineID, OverallScore: 70, Scores: map[string]float64{"length": 65},
	}
	repository.reports["workspace-1/"+currentID] = &evaluation.EvaluationReport{
		ID: currentID, OverallScore: 78, Scores: map[string]float64{"length": 75},
	}
	api := NewOperationsAPI(nil, evaluation.NewService(repository), nil, nil, "")
	response := sendEvaluationPolicyRequest(api, http.MethodPost,
		"/api/v2/evaluations/benchmarks/"+baselineID+"/compare", `{"current_id":"`+currentID+`","threshold":3}`)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"status":"improved"`) || !strings.Contains(response.Body.String(), `"overall_delta":8`) {
		t.Fatalf("comparison response = %d: %s", response.Code, response.Body.String())
	}
	response = sendEvaluationPolicyRequest(api, http.MethodPost,
		"/api/v2/evaluations/benchmarks/"+baselineID+"/compare", `{"current_id":"","threshold":3}`)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("invalid comparison status = %d: %s", response.Code, response.Body.String())
	}
	response = sendEvaluationPolicyRequest(api, http.MethodPost,
		"/api/v2/evaluations/benchmarks/"+baselineID+"/compare", `{"current_id":"`+baselineID+`"}`)
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"status":"similar"`) {
		t.Fatalf("default comparison threshold response = %d: %s", response.Code, response.Body.String())
	}
}

func TestBenchmarkListAcceptsSupportedMetadataFilters(t *testing.T) {
	repository := newEvaluationPolicyRepositoryStub()
	repository.benchmarks = []json.RawMessage{json.RawMessage(`{"id":"run-1"}`)}
	api := NewOperationsAPI(nil, evaluation.NewService(repository), nil, nil, "")
	response := sendEvaluationPolicyRequest(api, http.MethodGet,
		"/api/v2/evaluations/benchmarks?job_id=job-1&status=completed", "")
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"run-1"`) {
		t.Fatalf("filtered benchmark response = %d: %s", response.Code, response.Body.String())
	}
	if repository.filters.JobID != "job-1" || repository.filters.Status != "completed" {
		t.Fatalf("benchmark filters = %#v", repository.filters)
	}
}

func TestEvaluationAnalyticsAndDimensionsEndpoints(t *testing.T) {
	api := NewOperationsAPI(nil, evaluation.NewService(newEvaluationPolicyRepositoryStub()), nil, nil, "")
	response := sendEvaluationPolicyRequest(api, http.MethodGet, "/api/v2/evaluations/stats?since=2026-10-01T00:00:00Z", "")
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"total_reports":4`) {
		t.Fatalf("stats response = %d: %s", response.Code, response.Body.String())
	}
	response = sendEvaluationPolicyRequest(api, http.MethodGet, "/api/v2/evaluations/trends?dimension=length&limit=20", "")
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"direction":"stable"`) || !strings.Contains(response.Body.String(), `"score":82.5`) {
		t.Fatalf("trend response = %d: %s", response.Code, response.Body.String())
	}
	response = sendEvaluationPolicyRequest(api, http.MethodGet, "/api/v2/evaluations/trends?dimension=length&limit=0", "")
	if response.Code != http.StatusBadRequest {
		t.Fatalf("invalid trend status = %d: %s", response.Code, response.Body.String())
	}
	response = sendEvaluationPolicyRequest(api, http.MethodGet, "/api/v2/evaluations/dimensions?category=structure", "")
	if response.Code != http.StatusOK || !strings.Contains(response.Body.String(), `"length"`) {
		t.Fatalf("dimensions response = %d: %s", response.Code, response.Body.String())
	}
}

func sendEvaluationPolicyRequest(api http.Handler, method, path, body string) *httptest.ResponseRecorder {
	request := httptest.NewRequest(method, path, strings.NewReader(body))
	request = request.WithContext(identity.WithPrincipal(request.Context(), identity.Principal{WorkspaceID: "workspace-1", Role: "owner"}))
	response := httptest.NewRecorder()
	api.ServeHTTP(response, request)
	return response
}

func TestBenchmarkListOnlyDocumentsPersistedStatuses(t *testing.T) {
	response := sendEvaluationPolicyRequest(NewOperationsAPI(nil, evaluation.NewService(newEvaluationPolicyRepositoryStub()), nil, nil, ""),
		http.MethodGet, "/api/v2/evaluations/benchmarks?status=running", "")
	if response.Code != http.StatusBadRequest {
		t.Fatalf("running benchmark status filter response = %d: %s", response.Code, response.Body.String())
	}
}

func TestBenchmarkComparisonRejectsNonUUIDIdentifiers(t *testing.T) {
	response := sendEvaluationPolicyRequest(NewOperationsAPI(nil, evaluation.NewService(newEvaluationPolicyRepositoryStub()), nil, nil, ""),
		http.MethodPost, "/api/v2/evaluations/benchmarks/not-a-uuid/compare", `{"current_id":"also-not-a-uuid"}`)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("invalid benchmark ids response = %d: %s", response.Code, response.Body.String())
	}
}
