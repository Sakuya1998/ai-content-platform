package resources

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"

	"github.com/google/uuid"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/contracts"
	contentmodule "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/content"
	projectmodule "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project/transfer"
	taskmodule "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/task"
)

const maxResourceImportBytes int64 = 32 << 20

type ArtifactReader interface {
	// Get verifies an object key against its expected SHA256 digest.
	Get(context.Context, string, string) ([]byte, error)
}

type ResourceAPI struct {
	projects       Projects
	content        Content
	tasks          Tasks
	records        Records
	transfers      Transfers
	exports        *transfer.Service
	messages       Messaging
	submission     func(context.Context, string, projectmodule.Resource, string, map[string]any, string) (contracts.Command, error)
	normalizeError func(error) error
}

func NewHandler(dependencies Dependencies) *ResourceAPI {
	return &ResourceAPI{projects: dependencies.Projects, content: dependencies.Content, tasks: dependencies.Tasks, records: dependencies.Records, transfers: dependencies.Transfers, exports: dependencies.Exports, messages: dependencies.Messaging, submission: dependencies.SubmitCommand, normalizeError: dependencies.NormalizeError}
}

func (a *ResourceAPI) RecoverTransfers(ctx context.Context) error { return a.exports.Recover(ctx) }

type ProjectResource = projectmodule.Resource

func resourceHash(body []byte) string { sum := sha256.Sum256(body); return hex.EncodeToString(sum[:]) }
func resourceJSON(value any) []byte   { body, _ := json.Marshal(value); return body }
func resourceInt(value any) int {
	switch v := value.(type) {
	case float64:
		return int(v)
	case int:
		return v
	case json.Number:
		n, _ := strconv.Atoi(v.String())
		return n
	}
	return 0
}
func resourceText(value any) string { s, _ := value.(string); return s }
func (a *ResourceAPI) resourceError(w http.ResponseWriter, err error) {
	if a.normalizeError != nil {
		err = a.normalizeError(err)
	}
	status, message := 503, "resource store unavailable"
	switch {
	case errors.Is(err, ErrNotFound):
		status, message = 404, "resource not found"
	case errors.Is(err, ErrMessageConflict):
		status, message = 409, "resource version or active job conflict"
	case errors.Is(err, errResourceInvalid) || errors.Is(err, projectmodule.ErrInvalid) || errors.Is(err, taskmodule.ErrInvalid) || errors.Is(err, contentmodule.ErrInvalid) || errors.Is(err, transfer.ErrInvalid):
		status, message = 422, "invalid resource request"
	}
	writeJSON(w, status, map[string]string{"error": message})
}

var errResourceInvalid = errors.New("invalid resource request")

func normalizeCommonBrief(brief map[string]any) (map[string]any, error) {
	if brief == nil || len(resourceJSON(brief)) > 65536 {
		return nil, errResourceInvalid
	}
	return brief, nil
}

func readResourceBody(w http.ResponseWriter, r *http.Request, destination any) bool {
	if r.ContentLength > maxResourceImportBytes {
		writeJSON(w, http.StatusRequestEntityTooLarge, map[string]string{"error": "request body exceeds import limit"})
		return false
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxResourceImportBytes)
	decoder := json.NewDecoder(r.Body)
	if err := decoder.Decode(destination); err != nil {
		var tooLarge *http.MaxBytesError
		status := 400
		if errors.As(err, &tooLarge) {
			status = 413
		}
		writeJSON(w, status, map[string]string{"error": "invalid or oversized request body"})
		return false
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		writeJSON(w, 400, map[string]string{"error": "one JSON object required"})
		return false
	}
	return true
}

func (a *ResourceAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	scope := r.Header.Get("X-Workspace-ID")
	if _, err := uuid.Parse(scope); err != nil {
		writeJSON(w, 401, map[string]string{"error": "authenticated workspace required"})
		return
	}
	principal, authenticated := PrincipalFromContext(r.Context())
	if !authenticated || principal.WorkspaceID != scope {
		writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "authenticated workspace required"})
		return
	}
	if principal.Role != "owner" && principal.Role != "editor" && principal.Role != "viewer" {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "workspace membership required"})
		return
	}
	if r.Method != http.MethodGet && principal.Role == "viewer" {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "editor role required"})
		return
	}
	path := strings.TrimPrefix(r.URL.Path, "/api/")
	path = strings.TrimPrefix(path, "v2/")
	path = strings.TrimPrefix(path, "v1/")
	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) == 0 {
		http.NotFound(w, r)
		return
	}
	if parts[0] == "jobs" {
		a.jobHTTP(w, r, scope, parts[1:])
		return
	}
	if parts[0] == "transfers" {
		a.transferHTTP(w, r, scope, parts[1:])
		return
	}
	if parts[0] == "content" || parts[0] == "documents" {
		a.contentHTTP(w, r, scope, parts)
		return
	}
	if parts[0] != "projects" {
		http.NotFound(w, r)
		return
	}
	if len(parts) == 1 {
		switch r.Method {
		case "GET":
			a.listHTTP(w, r, scope)
		case "POST":
			a.createHTTP(w, r, scope)
		default:
			methodNotAllowed(w)
		}
		return
	}
	if parts[1] == "import" {
		a.importHTTP(w, r, scope)
		return
	}
	if _, err := uuid.Parse(parts[1]); err != nil {
		writeJSON(w, 404, map[string]string{"error": "project not found"})
		return
	}
	id := parts[1]
	novel, err := a.projects.Get(r.Context(), scope, id)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	if len(parts) == 2 {
		switch r.Method {
		case "GET":
			w.Header().Set("ETag", fmt.Sprintf("\"%d\"", novel.Revision))
			writeJSON(w, 200, novel)
		case "PUT":
			a.updateResourceHTTP(w, r, scope, novel)
		case "DELETE":
			a.deleteHTTP(w, r, scope, id)
		default:
			methodNotAllowed(w)
		}
		return
	}
	tail := parts[2:]
	switch tail[0] {
	case "projection":
		if r.Method != "GET" {
			methodNotAllowed(w)
			return
		}
		writeJSON(w, 200, novel.State)
	case "brief":
		a.briefHTTP(w, r, scope, novel, tail[1:])
	case "jobs":
		if len(tail) == 1 && r.Method == "GET" {
			a.listJobsHTTP(w, r, scope, id)
			return
		}
		if len(tail) != 2 || r.Method != "POST" {
			methodNotAllowed(w)
			return
		}
		a.operationHTTP(w, r, scope, novel, strings.ReplaceAll(tail[1], "-", "_"), nil)
	case "run", "resume":
		if r.Method != "POST" {
			methodNotAllowed(w)
			return
		}
		a.operationHTTP(w, r, scope, novel, tail[0], nil)
	case "blocks", "planning":
		a.versionHTTP(w, r, scope, novel, tail)
	case "audits":
		a.recordsHTTP(w, r, scope, id, "audit", 0, "", tail[1:])
	case "canon":
		if r.Method == "POST" {
			a.operationHTTP(w, r, scope, novel, "canon", nil)
		} else if r.Method == "GET" {
			writeJSON(w, 200, novel.State["canon"])
		} else {
			methodNotAllowed(w)
		}
	case "conflicts":
		a.conflictsHTTP(w, r, scope, novel)
	case "memory":
		if len(tail) == 1 && r.Method == "GET" {
			a.memoryHTTP(w, r, scope, novel)
			return
		}
		if len(tail) == 2 && tail[1] == "quality" {
			a.recordsHTTP(w, r, scope, id, "memory_quality", 0, "", nil)
			return
		}
		if len(tail) == 2 && r.Method == "POST" && (tail[1] == "evaluate" || tail[1] == "rebuild" || tail[1] == "search") {
			a.operationHTTP(w, r, scope, novel, "memory_"+tail[1], nil)
			return
		}
		http.NotFound(w, r)
	case "usage":
		a.usageHTTP(w, r, scope, id)
	case "traces":
		a.recordsHTTP(w, r, scope, id, "trace", 0, "", nil)
	case "export":
		a.exportHTTP(w, r, scope, novel, tail[1:])
	default:
		http.NotFound(w, r)
	}
}

func normalizeResourceInput(input map[string]any) error {
	if err := projectmodule.Normalize(input); err != nil {
		return errResourceInvalid
	}
	return nil
}
func (a *ResourceAPI) createHTTP(w http.ResponseWriter, r *http.Request, scope string) {
	var input map[string]any
	if !readResourceBody(w, r, &input) {
		return
	}
	if err := normalizeResourceInput(input); err != nil {
		a.resourceError(w, err)
		return
	}
	key := r.Header.Get("Idempotency-Key")
	if len(key) > 200 {
		a.resourceError(w, errResourceInvalid)
		return
	}
	novel, err := (projectmodule.Service{Repository: a.projects}).Create(r.Context(), scope, input, key)
	if errors.Is(err, projectmodule.ErrInvalid) {
		err = errResourceInvalid
	}
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 201, novel)
}
func (a *ResourceAPI) listHTTP(w http.ResponseWriter, r *http.Request, scope string) {
	limit := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		limit, _ = strconv.Atoi(raw)
	}
	if limit < 1 || limit > 100 {
		a.resourceError(w, errResourceInvalid)
		return
	}
	cursor := r.URL.Query().Get("cursor")
	if cursor != "" {
		if _, err := uuid.Parse(cursor); err != nil {
			a.resourceError(w, errResourceInvalid)
			return
		}
	}
	values, err := a.projects.List(r.Context(), scope, cursor, limit)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 200, values)
}

func (a *ResourceAPI) deleteHTTP(w http.ResponseWriter, r *http.Request, scope, id string) {
	err := a.projects.Delete(r.Context(), scope, id)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"deleted": true, "id": id})
}
func (a *ResourceAPI) briefHTTP(w http.ResponseWriter, r *http.Request, scope string, novel ProjectResource, tail []string) {
	if len(tail) == 1 && tail[0] == "versions" && r.Method == "GET" {
		a.briefVersionsHTTP(w, r, scope, novel.ID)
		return
	}
	if len(tail) > 0 || r.Method != "PUT" {
		methodNotAllowed(w)
		return
	}
	var req struct {
		Brief    map[string]any `json:"brief"`
		Expected int            `json:"expected_version"`
		Summary  string         `json:"change_summary"`
	}
	if !readResourceBody(w, r, &req) {
		return
	}
	if req.Brief == nil || len(req.Summary) > 500 {
		a.resourceError(w, errResourceInvalid)
		return
	}
	brief, err := normalizeCommonBrief(req.Brief)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	req.Brief = brief
	novel, changed, stale, err := a.projects.UpdateBrief(r.Context(), scope, novel.ID, req.Expected, r.Header.Get("If-Match"), req.Brief, req.Summary)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	result := map[string]any{}
	_ = json.Unmarshal(resourceJSON(novel), &result)
	result["changed"] = changed
	result["stale_candidate_count"] = stale
	result["requires_revalidation"] = changed
	w.Header().Set("ETag", fmt.Sprintf("\"%d\"", novel.Revision))
	writeJSON(w, 200, result)
}
func (a *ResourceAPI) briefVersionsHTTP(w http.ResponseWriter, r *http.Request, scope, id string) {
	values, err := a.projects.BriefVersions(r.Context(), scope, id)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 200, values)
}

func (a *ResourceAPI) operationHTTP(w http.ResponseWriter, r *http.Request, scope string, novel ProjectResource, operation string, args map[string]any) {
	if args == nil {
		if !readResourceBody(w, r, &args) {
			return
		}
	}
	if args == nil {
		args = map[string]any{}
	}
	receipt, err := (taskmodule.Service{Submitter: taskSubmitter{api: a, project: novel}}).Submit(r.Context(), scope, taskmodule.Request{ProjectID: novel.ID, Operation: operation, Arguments: args, IdempotencyKey: r.Header.Get("Idempotency-Key")})
	if err != nil {
		if errors.Is(err, taskmodule.ErrInvalid) {
			err = errResourceInvalid
		}
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 202, receipt)
}
func (a *ResourceAPI) submitOperation(ctx context.Context, scope string, resource ProjectResource, operation string, args map[string]any, key string) (contracts.Command, error) {
	return a.submission(ctx, scope, resource, operation, args, key)
}
