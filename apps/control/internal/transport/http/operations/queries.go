package operations

import (
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/evaluation"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/google/uuid"
)

func (a *OperationsAPI) benchmark(w http.ResponseWriter, r *http.Request, p identity.Principal) {
	id := strings.TrimPrefix(strings.TrimPrefix(canonicalPublicPath(r.URL.Path), "/api/evaluations/benchmarks"), "/")
	if r.Method == http.MethodPost && strings.HasSuffix(id, "/compare") {
		if a.evaluations == nil {
			operationError(w, errors.New("store unavailable"))
			return
		}
		baselineID := strings.TrimSuffix(id, "/compare")
		if baselineID == "" || strings.Contains(baselineID, "/") {
			http.NotFound(w, r)
			return
		}
		if _, err := uuid.Parse(baselineID); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "benchmark id must be a UUID"})
			return
		}
		var request struct {
			CurrentID string   `json:"current_id"`
			Threshold *float64 `json:"threshold"`
		}
		if !decodeJSON(w, r, &request) {
			return
		}
		threshold := 3.0
		if request.Threshold != nil {
			threshold = *request.Threshold
		}
		if _, err := uuid.Parse(request.CurrentID); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "current_id must be a UUID"})
			return
		}
		comparison, err := a.evaluations.CompareBenchmarks(r.Context(), p.WorkspaceID, baselineID, request.CurrentID, threshold)
		if err != nil {
			operationError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, comparison)
		return
	}
	if r.Method == http.MethodPost && id == "" {
		args := map[string]any{}
		if !readOperationArguments(w, r, &args) {
			return
		}
		project, _ := args["project_id"].(string)
		if _, err := uuid.Parse(project); err != nil {
			writeJSON(w, 400, map[string]string{"error": "project_id is required"})
			return
		}
		a.enqueue(w, r, p, project, "benchmark", args)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	if a.evaluations == nil {
		operationError(w, errors.New("store unavailable"))
		return
	}
	if id != "" {
		data, err := a.evaluations.Benchmark(r.Context(), p.WorkspaceID, id)
		if err != nil {
			operationError(w, err)
			return
		}
		writeJSON(w, 200, data)
		return
	}
	items, err := a.evaluations.ListBenchmarks(r.Context(), p.WorkspaceID, evaluation.BenchmarkFilters{
		JobID: r.URL.Query().Get("job_id"), Status: r.URL.Query().Get("status"),
	})
	if err != nil {
		operationError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"items": items})
}
func (a *OperationsAPI) latestProjection(w http.ResponseWriter, r *http.Request, workspace, project, kind string) {
	limit := 100
	if raw := r.URL.Query().Get("limit"); raw != "" {
		value, err := strconv.Atoi(raw)
		if err != nil || value < 1 || value > 500 {
			writeJSON(w, 400, map[string]string{"error": "limit must be between 1 and 500"})
			return
		}
		limit = value
	}
	if a.observability == nil {
		operationError(w, errors.New("store unavailable"))
		return
	}
	data, err := a.observability.Latest(r.Context(), workspace, project, kind, limit)
	if err != nil {
		operationError(w, err)
		return
	}
	writeJSON(w, 200, data)
}
func (a *OperationsAPI) listProjection(w http.ResponseWriter, r *http.Request, workspace, project, kind string) {
	if a.knowledge == nil {
		operationError(w, errors.New("store unavailable"))
		return
	}
	items, err := a.knowledge.List(r.Context(), workspace, project, kind)
	if err != nil {
		operationError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"items": items})
}
func (a *OperationsAPI) summary(w http.ResponseWriter, r *http.Request, p identity.Principal) {
	if a.observability == nil {
		operationError(w, errors.New("store unavailable"))
		return
	}
	value, err := a.observability.Summary(r.Context(), p.WorkspaceID)
	if err != nil {
		operationError(w, err)
		return
	}
	writeJSON(w, 200, value)
}
func (a *OperationsAPI) Metrics(w http.ResponseWriter, r *http.Request) {
	if a.internalToken == "" || r.Header.Get("Authorization") != "Bearer "+a.internalToken {
		writeJSON(w, 401, map[string]string{"error": "internal authentication required"})
		return
	}
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	if a.observability == nil || a.observability.Ready(r.Context()) != nil {
		_, _ = fmt.Fprintln(w, "agent_control_database_up 0")
		return
	}
	_, _ = fmt.Fprintln(w, "agent_control_database_up 1")
}
