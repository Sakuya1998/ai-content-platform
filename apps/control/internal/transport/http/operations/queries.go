package operations

import (
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/identity"
)

func (a *OperationsAPI) benchmark(w http.ResponseWriter, r *http.Request, p identity.Principal) {
	id := strings.TrimPrefix(strings.TrimPrefix(canonicalPublicPath(r.URL.Path), "/api/evaluations/benchmarks"), "/")
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
	items, err := a.evaluations.Benchmarks(r.Context(), p.WorkspaceID)
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
