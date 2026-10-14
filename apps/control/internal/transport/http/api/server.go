package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync/atomic"
	"time"

	"github.com/go-chi/chi/v5"
	projectmodule "github.com/sakuya/ai-content-platform/apps/control/internal/modules/project"
	projectmemory "github.com/sakuya/ai-content-platform/apps/control/internal/modules/project/memory"
)

var ErrNotFound = projectmodule.ErrNotFound

type ProjectRepository = projectmemory.ProjectRepository
type MemoryProjectRepository = projectmemory.MemoryProjectRepository

var NewMemoryProjectRepository = projectmemory.NewMemoryProjectRepository

type Server struct {
	projects         ProjectRepository
	sequence         atomic.Uint64
	artifacts        ArtifactSigner
	production       http.Handler
	identity         AuthenticatedHTTP
	governance       http.Handler
	resources        http.Handler
	router           http.Handler
	operations       http.Handler
	prompts          http.Handler
	creationPolicies http.Handler
	workflows        http.Handler
	ready            func(context.Context) error
}

type ServerDependencies struct {
	Projects         ProjectRepository
	Artifacts        ArtifactSigner
	Production       http.Handler
	Identity         AuthenticatedHTTP
	Governance       http.Handler
	Resources        http.Handler
	Operations       http.Handler
	Prompts          http.Handler
	CreationPolicies http.Handler
	Workflows        http.Handler
	Ready            func(context.Context) error
}

type AuthenticatedHTTP interface {
	http.Handler
	AuthenticatedHandler(http.Handler) http.Handler
}

func NewServerWithDependencies(deps ServerDependencies) *Server {
	projects := deps.Projects
	if projects == nil {
		projects = NewMemoryProjectRepository()
	}
	s := &Server{projects: projects, artifacts: deps.Artifacts, production: deps.Production, identity: deps.Identity, governance: deps.Governance, resources: deps.Resources, operations: deps.Operations, prompts: deps.Prompts, creationPolicies: deps.CreationPolicies, workflows: deps.Workflows, ready: deps.Ready}
	s.router = s.routes()
	return s
}
func NewHandler() http.Handler { return NewServer(NewMemoryProjectRepository()) }
func NewServer(projects ProjectRepository) *Server {
	return NewServerWithDependencies(ServerDependencies{Projects: projects})
}
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) { s.router.ServeHTTP(w, r) }

func (s *Server) routes() http.Handler {
	router := chi.NewRouter()
	router.Get("/healthz", s.healthHTTP)
	router.Get("/readyz", s.healthHTTP)
	if ops, ok := s.operations.(interface {
		Metrics(http.ResponseWriter, *http.Request)
	}); ok {
		router.Get("/metrics", ops.Metrics)
	}
	for _, prefix := range []string{"/api", "/api/v1", "/api/v2"} {
		router.Route(prefix, func(r chi.Router) {
			if s.identity != nil {
				r.Handle("/auth/*", s.identity)
				r.Handle("/workspaces", s.identity)
				r.Handle("/workspaces/{workspace}", s.identity)
				r.Handle("/workspaces/{workspace}/members", s.identity)
				r.Handle("/workspaces/{workspace}/members/*", s.identity)
				r.Handle("/audit/logs", s.identity)
			}
			authorized := func(handler http.Handler) http.Handler {
				if s.identity != nil {
					return s.identity.AuthenticatedHandler(handler)
				}
				return handler
			}
			attach := func(path string, handler http.Handler) {
				if handler != nil {
					r.Handle(path, authorized(handler))
					r.Handle(path+"/*", authorized(handler))
				}
			}
			if s.resources != nil {
				for _, path := range []string{"/projects", "/jobs", "/documents", "/content", "/transfers", "/evaluations"} {
					attach(path, s.resources)
				}
			} else {
				attach("/projects", http.HandlerFunc(s.projectHTTP))
			}
			if s.governance != nil {
				attach("/model-settings", s.governance)
				for _, kind := range []string{"providers", "model-profiles", "model-routes", "workflow-model-bindings", "content-types", "styles", "creative-templates", "quality-policies", "skills", "tools", "mcp-policies", "video-models", "image-models", "speech-models", "music-models", "sound-effect-models"} {
					attach("/workspaces/{workspace}/"+kind, s.governance)
				}
			}
			if s.prompts != nil {
				for _, kind := range []string{"prompts", "prompt-packs", "workflow-prompt-bindings"} {
					attach("/workspaces/{workspace}/"+kind, s.prompts)
				}
			}
			if s.creationPolicies != nil {
				for _, kind := range []string{"creation-policies", "workflow-creation-policy-bindings"} {
					attach("/workspaces/{workspace}/"+kind, s.creationPolicies)
				}
			}
			attach("/workspaces/{workspace}/workflows", s.workflows)
			if s.operations != nil {
				for _, path := range []string{"/knowledge-packs", "/evaluations/benchmarks", "/monitoring/summary", "/operations"} {
					attach(path, s.operations)
				}
				for _, path := range []string{"knowledge-packs", "usage", "traces"} {
					attach("/projects/{project}/"+path, s.operations)
				}
				for _, suffix := range []string{"", "/search", "/refresh"} {
					r.Handle("/projects/{project}/memory"+suffix, authorized(s.operations))
				}
			}
			if s.production != nil {
				attach("/projects/{project}/productions", s.production)
			}
		})
	}
	return router
}

func (s *Server) healthHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/readyz" {
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()
		ready := s.ready
		if ready == nil && s.artifacts != nil {
			ready = s.artifacts.Ready
		}
		if ready != nil && ready(ctx) != nil {
			writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "dependencies unavailable"})
			return
		}
	}
	writeJSON(w, 200, map[string]string{"status": "ok"})
}

func (s *Server) projectHTTP(w http.ResponseWriter, r *http.Request) {
	path := canonicalPublicPath(r.URL.Path)
	workspaceID := r.Header.Get("X-Workspace-ID")
	const prefix = "/api/v2/projects"
	if path == "/api/projects" || strings.HasPrefix(path, "/api/projects/") {
		copy := r.Clone(r.Context())
		copy.URL.Path = prefix + strings.TrimPrefix(path, "/api/projects")
		r = copy
	}
	if r.URL.Path == prefix || r.URL.Path == prefix+"/" {
		if r.Method == http.MethodGet {
			projects, err := s.projects.List(workspaceID)
			if err != nil {
				writeRepositoryError(w, err)
				return
			}
			writeJSON(w, http.StatusOK, projects)
			return
		}
		if r.Method == http.MethodPost {
			s.createProject(w, r)
			return
		}
		methodNotAllowed(w)
		return
	}
	if !strings.HasPrefix(r.URL.Path, prefix+"/") {
		http.NotFound(w, r)
		return
	}
	id := strings.TrimPrefix(r.URL.Path, prefix+"/")
	if id == "" || strings.Contains(id, "/") {
		http.NotFound(w, r)
		return
	}
	switch r.Method {
	case http.MethodGet:
		project, err := s.projects.Get(workspaceID, id)
		if err != nil {
			writeRepositoryError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, project)
	case http.MethodPut:
		s.updateProject(w, r, id)
	case http.MethodDelete:
		if err := s.projects.Delete(workspaceID, id); err != nil {
			writeRepositoryError(w, err)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	default:
		methodNotAllowed(w)
	}
}

type projectRequest struct {
	WorkspaceID string `json:"workspace_id"`
	ProjectType string `json:"project_type"`
	ContentType string `json:"content_type"`
	Name        string `json:"name"`
	Description string `json:"description"`
}

func (s *Server) createProject(w http.ResponseWriter, r *http.Request) {
	var request projectRequest
	if !decodeJSON(w, r, &request) {
		return
	}
	if strings.TrimSpace(request.Name) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "name is required"})
		return
	}
	if strings.TrimSpace(request.WorkspaceID) == "" {
		request.WorkspaceID = r.Header.Get("X-Workspace-ID")
	}
	if principal, ok := PrincipalFromContext(r.Context()); ok && request.WorkspaceID != principal.WorkspaceID {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "workspace access denied"})
		return
	}
	now := time.Now().UTC()
	projectType := strings.TrimSpace(request.ProjectType)
	if request.ContentType != "" {
		projectType = strings.TrimSpace(request.ContentType)
	}
	if projectType == "" {
		projectType = "generic"
	}
	project := projectmodule.Project{ID: fmt.Sprintf("project-%d", s.sequence.Add(1)), WorkspaceID: strings.TrimSpace(request.WorkspaceID), ProjectType: projectType, Name: strings.TrimSpace(request.Name), Description: request.Description, CreatedAt: now, UpdatedAt: now}
	created, err := s.projects.Create(project)
	if err != nil {
		writeRepositoryError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, created)
}

func (s *Server) updateProject(w http.ResponseWriter, r *http.Request, id string) {
	var request projectRequest
	if !decodeJSON(w, r, &request) {
		return
	}
	if strings.TrimSpace(request.Name) == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "name is required"})
		return
	}
	project, err := s.projects.Update(r.Header.Get("X-Workspace-ID"), id, strings.TrimSpace(request.Name), request.Description)
	if err != nil {
		writeRepositoryError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, project)
}

func writeRepositoryError(w http.ResponseWriter, err error) {
	if errors.Is(err, ErrNotFound) {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "project not found"})
		return
	}
	writeJSON(w, http.StatusServiceUnavailable, map[string]string{"error": "project store unavailable"})
}

func decodeJSON(w http.ResponseWriter, r *http.Request, destination any) bool {
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(destination); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid request body"})
		return false
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "request body must contain one JSON object"})
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func methodNotAllowed(w http.ResponseWriter) {
	w.Header().Set("Allow", "GET, POST, PUT, DELETE")
	writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
}
