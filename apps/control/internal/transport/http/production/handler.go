package productionhttp

import (
	"github.com/google/uuid"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/production"
	projectmodule "github.com/sakuya/novel-agent/go-control-plane/internal/modules/project"
	"github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/response"
	"net/http"
	"strings"
)

type Handler struct{ service production.Service }

func NewHandler(service production.Service) *Handler { return &Handler{service: service} }

var writeJSON = response.JSON
var decodeJSON = response.DecodeJSON
var methodNotAllowed = response.MethodNotAllowed
var writeMQError = response.ResourceError
var canonicalPublicPath = response.CanonicalPath

func (s *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	workspace := r.Header.Get("X-Workspace-ID")
	project := ""
	suffix := ""

	// Public routes pass through IdentityAPI.AuthenticatedHandler, including CSRF and role checks.
	parts := strings.Split(strings.TrimPrefix(canonicalPublicPath(r.URL.Path), "/api/projects/"), "/")
	if len(parts) < 2 || parts[1] != "productions" {
		http.NotFound(w, r)
		return
	}
	project = parts[0]
	suffix = strings.Join(parts[2:], "/")
	if _, e := uuid.Parse(project); e != nil {
		writeJSON(w, 400, map[string]string{"error": "invalid project ID"})
		return
	}
	if _, e := uuid.Parse(workspace); e != nil {
		writeJSON(w, 400, map[string]string{"error": "valid workspace ID required"})
		return
	}
	if s.service.Repository == nil {
		writeJSON(w, 503, map[string]string{"error": "persistent messaging store unavailable"})
		return
	}
	if suffix == "" {
		switch r.Method {
		case http.MethodPost:
			request := production.Request{Subtitles: true, GenerateSpeech: true}
			r.Body = http.MaxBytesReader(w, r.Body, 256*1024)
			if !decodeJSON(w, r, &request) {
				return
			}
			{
				if request.ProjectID != "" && request.ProjectID != project {
					writeJSON(w, 400, map[string]string{"error": "project does not match route"})
					return
				}
				request.ProjectID = project
			}
			production.Normalize(&request)
			if !production.Valid(request) {
				writeJSON(w, 400, map[string]string{"error": "invalid production brief"})
				return
			}
			result, e := s.service.Create(r.Context(), workspace, request)
			if e != nil {
				writeMQError(w, e)
				return
			}
			writeJSON(w, 202, result)
		case http.MethodGet:
			if project != "" {
				if _, e := uuid.Parse(project); e != nil {
					writeJSON(w, 400, map[string]string{"error": "invalid project ID"})
					return
				}
			}
			result, e := s.service.List(r.Context(), workspace, project)
			if e != nil {
				writeMQError(w, e)
				return
			}
			writeJSON(w, 200, result)
		default:
			methodNotAllowed(w)
		}
		return
	}
	parts = strings.Split(suffix, "/")
	id := parts[0]
	if _, e := uuid.Parse(id); e != nil {
		writeJSON(w, 400, map[string]string{"error": "invalid run ID"})
		return
	}
	run, e := s.service.Get(r.Context(), workspace, id)
	if e != nil {
		writeMQError(w, e)
		return
	}
	if run.ProjectID != project {
		writeMQError(w, projectmodule.ErrNotFound)
		return
	}
	if len(parts) == 1 && r.Method == http.MethodGet {
		writeJSON(w, 200, run)
		return
	}
	if len(parts) != 2 {
		http.NotFound(w, r)
		return
	}
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	var result production.Run
	switch parts[1] {
	case "advance":
		result, e = s.service.Advance(r.Context(), workspace, id)
	case "cancel":
		result, e = s.service.Cancel(r.Context(), workspace, id)
	case "resume":
		var request struct {
			ChildJobID string `json:"child_job_id"`
			Action     string `json:"action"`
		}
		r.Body = http.MaxBytesReader(w, r.Body, 4096)
		if !decodeJSON(w, r, &request) {
			return
		}
		result, e = s.service.Resume(r.Context(), workspace, id, request.ChildJobID, request.Action)
	default:
		http.NotFound(w, r)
		return
	}
	if e != nil {
		writeMQError(w, e)
		return
	}
	writeJSON(w, 202, result)
}
