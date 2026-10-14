package workflowhttp

import (
	"errors"
	"net/http"
	"strings"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/workflow"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/transport/http/response"
)

type Handler struct{ service *workflow.Service }

func NewHandler(service *workflow.Service) *Handler { return &Handler{service: service} }
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	p, ok := identity.FromContext(r.Context())
	if !ok {
		response.JSON(w, 401, map[string]string{"error": "authentication required"})
		return
	}
	parts := strings.Split(strings.Trim(strings.TrimPrefix(response.CanonicalPath(r.URL.Path), "/api/workspaces/"), "/"), "/")
	if len(parts) < 2 || parts[1] != "workflows" {
		http.NotFound(w, r)
		return
	}
	if parts[0] != p.WorkspaceID {
		response.JSON(w, 403, map[string]string{"error": "workspace access denied"})
		return
	}
	var result any
	var err error
	status := 200
	switch {
	case len(parts) == 2 && r.Method == http.MethodGet:
		var items []workflow.Version
		items, err = h.service.List(r.Context(), p)
		result = map[string]any{"items": items}
	case len(parts) == 2 && r.Method == http.MethodPost:
		var input struct {
			Name       string              `json:"name"`
			Definition workflow.Definition `json:"definition"`
		}
		r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
		if !response.DecodeJSON(w, r, &input) {
			return
		}
		result, err = h.service.Create(r.Context(), p, input.Name, input.Definition)
		status = 201
	case len(parts) == 3 && r.Method == http.MethodGet:
		result, err = h.service.Get(r.Context(), p, parts[2])
	case len(parts) == 4 && r.Method == http.MethodPost && parts[3] == "publish":
		result, err = h.service.Publish(r.Context(), p, parts[2])
	case len(parts) == 4 && r.Method == http.MethodPost && parts[3] == "disable":
		result, err = h.service.Disable(r.Context(), p, parts[2])
	default:
		response.MethodNotAllowed(w)
		return
	}
	if err != nil {
		status = 503
		switch {
		case errors.Is(err, identity.ErrForbidden):
			status = 403
		case errors.Is(err, identity.ErrNotFound):
			status = 404
		case errors.Is(err, identity.ErrConflict):
			status = 409
		case errors.Is(err, workflow.ErrInvalid):
			status = 422
		}
		response.JSON(w, status, map[string]string{"error": "workflow operation failed"})
		return
	}
	response.JSON(w, status, result)
}
