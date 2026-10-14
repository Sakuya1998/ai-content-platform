package api

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	projectmodule "github.com/sakuya/novel-agent/go-control-plane/internal/modules/project"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/objectstorage"
)

type testSigner struct{ key string }

func (s *testSigner) PresignUpload(_ context.Context, key, digest, contentType string, expiry time.Duration) (objectstorage.SignedURL, error) {
	s.key = key
	return objectstorage.SignedURL{URL: "http://storage/signed", ObjectKey: key}, nil
}
func (s *testSigner) PresignDownload(_ context.Context, key string, expiry time.Duration) (objectstorage.SignedURL, error) {
	s.key = key
	return objectstorage.SignedURL{URL: "http://storage/signed", ObjectKey: key}, nil
}
func (s *testSigner) Ready(context.Context) error { return nil }

func TestArtifactSigningRequiresTokenAndMatchingProject(t *testing.T) {
	repo := NewMemoryProjectRepository()
	_, _ = repo.Create(projectmodule.Project{ID: "p-1", WorkspaceID: "ws-1"})
	signer := &testSigner{}
	handler := NewServerWithDependencies(ServerDependencies{Projects: repo, Artifacts: signer})
	for _, tc := range []struct {
		token, workspace string
		expected         int
	}{
		{"", "ws-1", 404}, {"Bearer internal-test-token", "ws-2", 404}, {"Bearer internal-test-token", "ws-1", 404},
	} {
		body := `{"project_id":"p-1","category":"uploads","resource_id":"a-1","filename":"source.txt","operation":"upload","sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","content_type":"text/plain"}`
		r := httptest.NewRequest(http.MethodPost, "/internal/v1/artifacts/sign", bytes.NewBufferString(body))
		r.Header.Set("Authorization", tc.token)
		r.Header.Set("X-Workspace-ID", tc.workspace)
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, r)
		if w.Code != tc.expected {
			t.Fatalf("signing status %d: %s", w.Code, w.Body.String())
		}
	}
	if signer.key != "" {
		t.Fatal("retired service endpoint signed an object")
	}
}
