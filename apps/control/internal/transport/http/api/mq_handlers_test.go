package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestInternalJobsRequireAuthentication(t *testing.T) {
	server := NewServerWithDependencies(ServerDependencies{})
	request := httptest.NewRequest(http.MethodPost, "/internal/v1/jobs", nil)
	response := httptest.NewRecorder()
	server.ServeHTTP(response, request)
	if response.Code != http.StatusNotFound {
		t.Fatalf("unauthenticated job endpoint: %d", response.Code)
	}
	request = httptest.NewRequest(http.MethodPost, "/internal/v1/jobs", nil)
	request.Header.Set("Authorization", "Bearer test-token")
	response = httptest.NewRecorder()
	server.ServeHTTP(response, request)
	if response.Code != http.StatusNotFound {
		t.Fatalf("workspace is required: %d", response.Code)
	}
}
