package operations

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/contracts"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/evaluation"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/knowledge"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/observability"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/task"
	"github.com/google/uuid"
)

type OperationSubmitter interface {
	SubmitOperation(context.Context, string, string, string, string, map[string]any) (contracts.Command, error)
}
type OperationsAPI struct {
	knowledge     *knowledge.Service
	evaluations   *evaluation.Service
	observability *observability.Service
	messages      OperationSubmitter
	internalToken string
}

func NewOperationsAPI(knowledgeService *knowledge.Service, evaluationService *evaluation.Service, observabilityService *observability.Service, messages OperationSubmitter, token string) *OperationsAPI {
	return &OperationsAPI{knowledge: knowledgeService, evaluations: evaluationService, observability: observabilityService, messages: messages, internalToken: token}
}
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func decodeJSON(w http.ResponseWriter, r *http.Request, value any) bool {
	return decodeJSONWithLimit(w, r, value, 1<<20)
}

func decodeJSONWithLimit(w http.ResponseWriter, r *http.Request, value any, limit int64) bool {
	r.Body = http.MaxBytesReader(w, r.Body, limit)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		writeJSON(w, requestBodyErrorStatus(err), map[string]string{"error": "invalid request body"})
		return false
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		writeJSON(w, requestBodyErrorStatus(err), map[string]string{"error": "invalid request body"})
		return false
	}
	return true
}

func requestBodyErrorStatus(err error) int {
	var tooLarge *http.MaxBytesError
	if errors.As(err, &tooLarge) {
		return http.StatusRequestEntityTooLarge
	}
	return http.StatusBadRequest
}
func canonicalPublicPath(path string) string {
	for _, prefix := range []string{"/api/v2/", "/api/v1/"} {
		if strings.HasPrefix(path, prefix) {
			return "/api/" + strings.TrimPrefix(path, prefix)
		}
	}
	return strings.TrimSuffix(path, "/")
}
func methodNotAllowed(w http.ResponseWriter) {
	writeJSON(w, 405, map[string]string{"error": "method not allowed"})
}
func operationError(w http.ResponseWriter, err error) {
	status := 503
	message := "operation unavailable"
	switch {
	case errors.Is(err, evaluation.ErrInvalid):
		status, message = 400, "invalid evaluation request"
	case errors.Is(err, evaluation.ErrNotFound), errors.Is(err, identity.ErrNotFound), errors.Is(err, project.ErrNotFound):
		status = 404
		message = "resource not found"
	case errors.Is(err, evaluation.ErrConflict), errors.Is(err, task.ErrConflict):
		status, message = 409, "resource conflict"
	}
	writeJSON(w, status, map[string]string{"error": message})
}

func (a *OperationsAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	p, ok := identity.FromContext(r.Context())
	if !ok {
		writeJSON(w, 401, map[string]string{"error": "authentication required"})
		return
	}
	if p.Role != "owner" && p.Role != "editor" && p.Role != "viewer" || r.Method != http.MethodGet && p.Role == "viewer" {
		writeJSON(w, 403, map[string]string{"error": "editor role required"})
		return
	}
	path := canonicalPublicPath(r.URL.Path)
	if path == "/api/evaluations/policies" || strings.HasPrefix(path, "/api/evaluations/policies/") {
		a.evaluationPolicies(w, r, p, path)
		return
	}
	if path == "/api/evaluations/stats" || path == "/api/evaluations/trends" || path == "/api/evaluations/dimensions" || path == "/api/evaluations/issues" {
		a.evaluationAnalytics(w, r, p, path)
		return
	}
	if path == "/api/monitoring/summary" {
		if r.Method != http.MethodGet {
			methodNotAllowed(w)
			return
		}
		a.summary(w, r, p)
		return
	}
	if path == "/api/evaluations/benchmarks" || strings.HasPrefix(path, "/api/evaluations/benchmarks/") {
		a.benchmark(w, r, p)
		return
	}
	if path == "/api/knowledge-packs" {
		if r.Method != http.MethodGet {
			methodNotAllowed(w)
			return
		}
		a.listProjection(w, r, p.WorkspaceID, "", "knowledge_pack")
		return
	}
	parts := strings.Split(strings.TrimPrefix(path, "/api/"), "/")
	if len(parts) < 3 || parts[0] != "projects" {
		http.NotFound(w, r)
		return
	}
	project := parts[1]
	if _, err := uuid.Parse(project); err != nil {
		http.NotFound(w, r)
		return
	}
	operation := ""
	arguments := map[string]any{}
	switch parts[2] {
	case "memory":
		if r.Method == http.MethodGet && len(parts) == 3 {
			a.latestProjection(w, r, p.WorkspaceID, project, "runtime_memory")
			return
		}
		if r.Method == http.MethodPost && len(parts) == 4 && (parts[3] == "search" || parts[3] == "refresh") {
			operation = "memory." + parts[3]
			if !readOperationArguments(w, r, &arguments) {
				return
			}
			if operation == "memory.search" {
				query, ok := arguments["query"].(string)
				if !ok || strings.TrimSpace(query) == "" || len(query) > 10000 {
					writeJSON(w, 400, map[string]string{"error": "query is required"})
					return
				}
				if limit, exists := arguments["limit"]; exists {
					n, ok := limit.(float64)
					if !ok || n < 1 || n > 100 || n != float64(int(n)) {
						writeJSON(w, 400, map[string]string{"error": "limit must be an integer between 1 and 100"})
						return
					}
				}
			}
		}
	case "usage", "traces":
		if r.Method == http.MethodGet && len(parts) == 3 {
			a.latestProjection(w, r, p.WorkspaceID, project, "runtime_"+parts[2])
			return
		}
	case "knowledge-packs":
		if r.Method == http.MethodGet && len(parts) == 3 {
			a.listProjection(w, r, p.WorkspaceID, project, "knowledge_binding")
			return
		}
		if r.Method == http.MethodPost && len(parts) == 3 {
			operation = "knowledge.publish"
			if !readOperationArguments(w, r, &arguments) {
				return
			}
			name, ok := arguments["name"].(string)
			if !ok || strings.TrimSpace(name) == "" || len(name) > 160 {
				writeJSON(w, 400, map[string]string{"error": "name is required"})
				return
			}
		}
		if len(parts) == 4 && (r.Method == http.MethodPut || r.Method == http.MethodDelete) {
			if _, err := uuid.Parse(parts[3]); err != nil {
				http.NotFound(w, r)
				return
			}
			operation = "knowledge.bind"
			if r.Method == http.MethodDelete {
				operation = "knowledge.unbind"
			}
			arguments["pack_id"] = parts[3]
		}
	}
	if operation == "" {
		http.NotFound(w, r)
		return
	}
	a.enqueue(w, r, p, project, operation, arguments)
}

func readOperationArguments(w http.ResponseWriter, r *http.Request, arguments *map[string]any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
	if !decodeJSON(w, r, arguments) {
		return false
	}
	if *arguments == nil {
		writeJSON(w, 400, map[string]string{"error": "operation arguments must be an object"})
		return false
	}
	return true
}

func (a *OperationsAPI) enqueue(w http.ResponseWriter, r *http.Request, p identity.Principal, project, operation string, arguments map[string]any) {
	key := r.Header.Get("Idempotency-Key")
	if key == "" {
		key = uuid.NewString()
	}
	if len(key) > 200 {
		writeJSON(w, 400, map[string]string{"error": "idempotency key is too long"})
		return
	}
	command, err := a.messages.SubmitOperation(r.Context(), p.WorkspaceID, project, operation, key, arguments)
	if err != nil {
		operationError(w, err)
		return
	}
	w.Header().Set("Location", "/api/v2/jobs/"+command.JobID)
	writeJSON(w, http.StatusAccepted, map[string]any{"operation_id": command.JobID, "job_id": command.JobID, "status": "queued"})
}
