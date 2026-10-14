package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealthAndReadiness(t *testing.T) {
	handler := NewHandler()
	for path := range map[string]struct{}{"/healthz": {}, "/readyz": {}} {
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
		if response.Code != http.StatusOK {
			t.Fatalf("GET %s status = %d, want 200", path, response.Code)
		}
		if response.Header().Get("Content-Type") != "application/json" {
			t.Fatalf("GET %s content type = %q", path, response.Header().Get("Content-Type"))
		}
	}
}

func TestReadinessRejectsUnavailableDatabaseWithoutFailingLiveness(t *testing.T) {
	checks := 0
	server := NewServerWithDependencies(ServerDependencies{Ready: func(ctx context.Context) error {
		checks++
		if _, ok := ctx.Deadline(); !ok {
			t.Fatal("readiness check has no deadline")
		}
		return errors.New("database connection unavailable")
	}})
	for _, path := range []string{"/healthz", "/readyz"} {
		response := httptest.NewRecorder()
		server.ServeHTTP(response, httptest.NewRequest("GET", path, nil))
		want := 200
		if path == "/readyz" {
			want = 503
		}
		if response.Code != want {
			t.Fatalf("%s=%d want%d", path, response.Code, want)
		}
		if bytes.Contains(response.Body.Bytes(), []byte("database connection")) {
			t.Fatal("readiness exposes dependency failure")
		}
	}
	if checks != 1 {
		t.Fatalf("dependency checks=%d", checks)
	}
}

func TestMemoryOperationRoutesPreserveProjectQualityAndRebuild(t *testing.T) {
	resource := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Owner", "resource")
		w.WriteHeader(200)
	})
	operations := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Owner", "operations")
		w.WriteHeader(200)
	})
	server := NewServerWithDependencies(ServerDependencies{Resources: resource, Operations: operations})
	for _, tc := range []struct{ suffix, owner string }{
		{"", "operations"}, {"/search", "operations"}, {"/refresh", "operations"},
		{"/quality", "resource"}, {"/rebuild", "resource"}, {"/evaluate", "resource"},
	} {
		response := httptest.NewRecorder()
		server.ServeHTTP(response, httptest.NewRequest("GET", "/api/v2/projects/p-1/memory"+tc.suffix, nil))
		if response.Header().Get("X-Owner") != tc.owner {
			t.Fatalf("memory%s dispatched to %s", tc.suffix, response.Header().Get("X-Owner"))
		}
	}
}

func TestGenericExactRoutesReachTheirResourceHandlers(t *testing.T) {
	handler := func(owner string) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Header().Set("X-Owner", owner); w.WriteHeader(200) })
	}
	server := NewServerWithDependencies(ServerDependencies{Resources: handler("resources"), Production: handler("production"), Workflows: handler("workflow")})
	for _, tc := range []struct{ path, owner string }{
		{"/api/v2/projects/import", "resources"},
		{"/api/v2/documents/document-id/versions", "resources"},
		{"/api/v2/jobs/job-id/resume", "resources"},
		{"/api/v2/projects/project-id/productions/production-id/advance", "production"},
		{"/api/v2/workspaces/workspace-id/workflows", "workflow"},
		{"/api/v2/workspaces/workspace-id/workflows/workflow-id/publish", "workflow"},
		{"/api/v2/workspaces/workspace-id/workflows/workflow-id/disable", "workflow"},
	} {
		response := httptest.NewRecorder()
		server.ServeHTTP(response, httptest.NewRequest("POST", tc.path, nil))
		if response.Header().Get("X-Owner") != tc.owner {
			t.Fatalf("%s owner=%s want%s", tc.path, response.Header().Get("X-Owner"), tc.owner)
		}
	}
}

func TestProjectCRUD(t *testing.T) {
	handler := NewHandler()
	created := request(t, handler, http.MethodPost, "/api/v2/projects", `{"name":"Novel","description":"A draft"}`)
	if created.Code != http.StatusCreated {
		t.Fatalf("create status = %d, want 201: %s", created.Code, created.Body.String())
	}
	var project struct {
		ID          string `json:"id"`
		Name        string `json:"name"`
		Description string `json:"description"`
	}
	if err := json.Unmarshal(created.Body.Bytes(), &project); err != nil {
		t.Fatal(err)
	}
	if project.ID == "" || project.Name != "Novel" || project.Description != "A draft" {
		t.Fatalf("unexpected project response: %+v", project)
	}

	listed := request(t, handler, http.MethodGet, "/api/v2/projects", "")
	if listed.Code != http.StatusOK || !bytes.Contains(listed.Body.Bytes(), []byte(project.ID)) {
		t.Fatalf("list response = %d %s", listed.Code, listed.Body.String())
	}

	updated := request(t, handler, http.MethodPut, "/api/v2/projects/"+project.ID, `{"name":"Revised"}`)
	if updated.Code != http.StatusOK || !bytes.Contains(updated.Body.Bytes(), []byte(`"name":"Revised"`)) {
		t.Fatalf("update response = %d %s", updated.Code, updated.Body.String())
	}

	got := request(t, handler, http.MethodGet, "/api/v2/projects/"+project.ID, "")
	if got.Code != http.StatusOK || !bytes.Contains(got.Body.Bytes(), []byte(`"name":"Revised"`)) {
		t.Fatalf("get response = %d %s", got.Code, got.Body.String())
	}

	deleted := request(t, handler, http.MethodDelete, "/api/v2/projects/"+project.ID, "")
	if deleted.Code != http.StatusNoContent {
		t.Fatalf("delete status = %d, want 204", deleted.Code)
	}
	missing := request(t, handler, http.MethodGet, "/api/v2/projects/"+project.ID, "")
	if missing.Code != http.StatusNotFound {
		t.Fatalf("get deleted status = %d, want 404", missing.Code)
	}
}

func TestCreateProjectValidatesRequest(t *testing.T) {
	handler := NewHandler()
	for _, body := range []string{`{`, `{"name":"  "}`, `{"name":"ok","extra":true}`} {
		response := request(t, handler, http.MethodPost, "/api/v2/projects", body)
		if response.Code != http.StatusBadRequest {
			t.Errorf("body %q status = %d, want 400", body, response.Code)
		}
	}
}

func TestProjectListsAreWorkspaceScoped(t *testing.T) {
	handler := NewHandler()
	create := func(workspace, name string) {
		request := httptest.NewRequest(http.MethodPost, "/api/v2/projects", bytes.NewBufferString(`{"name":"`+name+`"}`))
		request.Header.Set("X-Workspace-ID", workspace)
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, request)
		if response.Code != http.StatusCreated {
			t.Fatalf("create status = %d", response.Code)
		}
	}
	create("workspace-a", "A")
	create("workspace-b", "B")

	request := httptest.NewRequest(http.MethodGet, "/api/v2/projects", nil)
	request.Header.Set("X-Workspace-ID", "workspace-a")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)

	if response.Code != http.StatusOK || bytes.Contains(response.Body.Bytes(), []byte(`"name":"B"`)) || !bytes.Contains(response.Body.Bytes(), []byte(`"name":"A"`)) {
		t.Fatalf("workspace list = %d %s", response.Code, response.Body.String())
	}
}

func request(t *testing.T, handler http.Handler, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(method, path, bytes.NewBufferString(body)))
	return response
}
