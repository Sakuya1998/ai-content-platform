package governancehttp

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"

	"github.com/sakuya/novel-agent/apps/control/internal/modules/governance"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/identity"
)

type Authenticator interface {
	AuthenticatedHandler(http.Handler) http.Handler
}
type Handler struct {
	service  *governance.CatalogService
	identity Authenticator
	catalog  string
}

func NewHandler(service *governance.CatalogService, auth Authenticator, catalog string) *Handler {
	return &Handler{service: service, identity: auth, catalog: catalog}
}
func canonicalPath(path string) string {
	for _, prefix := range []string{"/api/v2/", "/api/v1/"} {
		if strings.HasPrefix(path, prefix) {
			return "/api/" + strings.TrimPrefix(path, prefix)
		}
	}
	return strings.TrimSuffix(path, "/")
}
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if value != nil {
		_ = json.NewEncoder(w).Encode(value)
	}
}
func reject(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if strings.HasPrefix(r.URL.Path, "/internal/") {
		http.NotFound(w, r)
		return
	}
	handler := http.HandlerFunc(h.public)
	if h.identity != nil {
		h.identity.AuthenticatedHandler(handler).ServeHTTP(w, r)
	} else {
		handler.ServeHTTP(w, r)
	}
}
func (h *Handler) public(w http.ResponseWriter, r *http.Request) {
	p, ok := identity.FromContext(r.Context())
	if !ok {
		reject(w, 401, "authentication required")
		return
	}
	action := map[string]governance.Action{"GET": governance.Read, "POST": governance.Create, "PUT": governance.Replace, "PATCH": governance.Update, "DELETE": governance.Delete}[r.Method]
	if action == "" {
		reject(w, 405, "method not allowed")
		return
	}
	if action != governance.Read && p.Role != "owner" {
		reject(w, 403, "owner role required")
		return
	}
	path := canonicalPath(r.URL.Path)
	command := governance.CatalogCommand{Action: action, Filters: map[string]string{}}
	for _, key := range []string{"status", "version"} {
		command.Filters[key] = r.URL.Query().Get(key)
	}
	if action != governance.Read {
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 2<<20))
		if err != nil {
			reject(w, 413, "catalog request too large")
			return
		}
		command.Payload = body
	}
	var result governance.CatalogResult
	var err error
	if strings.HasPrefix(path, "/api/model-settings") && h.catalog == "resources" {
		tail := strings.Trim(strings.TrimPrefix(path, "/api/model-settings"), "/")
		if tail != "" {
			command.Path = strings.Split(tail, "/")
		}
		result, err = h.service.Model(r.Context(), p, command)
	} else {
		if !strings.HasPrefix(path, "/api/workspaces/") {
			http.NotFound(w, r)
			return
		}
		parts := strings.Split(strings.Trim(strings.TrimPrefix(path, "/api/workspaces/"), "/"), "/")
		if len(parts) < 2 {
			http.NotFound(w, r)
			return
		}
		if parts[0] != p.WorkspaceID {
			reject(w, 403, "workspace access denied")
			return
		}
		command.Path = parts[2:]
		switch h.catalog {
		case "resources":
			kind := governance.NormalizeKind(parts[1])
			if kind == "" {
				http.NotFound(w, r)
				return
			}
			result, err = h.service.Resource(r.Context(), p, kind, command)
		case "prompts":
			if parts[1] != "prompts" && parts[1] != "prompt-packs" && parts[1] != "prompt_packs" && parts[1] != "workflow-prompt-bindings" && parts[1] != "workflow_prompt_bindings" {
				http.NotFound(w, r)
				return
			}
			result, err = h.service.Prompt(r.Context(), p, parts[1], command)
		case "policies":
			if parts[1] != "creation-policies" && parts[1] != "workflow-creation-policy-bindings" {
				http.NotFound(w, r)
				return
			}
			result, err = h.service.Policy(r.Context(), p, parts[1], command)
		}
	}
	if err != nil {
		switch {
		case errors.Is(err, identity.ErrUnauthorized):
			reject(w, 401, "authentication required")
		case errors.Is(err, identity.ErrForbidden):
			reject(w, 403, "owner role required")
		default:
			reject(w, 503, "storage unavailable")
		}
		return
	}
	status := map[string]int{"ok": 200, "created": 201, "empty": 204, "bad_request": 400, "unauthorized": 401, "forbidden": 403, "missing": 404, "unsupported": 405, "conflict": 409, "too_large": 413, "invalid": 422, "unavailable": 503}[result.Kind]
	if status == 0 {
		status = 503
	}
	writeJSON(w, status, result.Value)
}
