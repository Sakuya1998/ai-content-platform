package http

import (
	"bytes"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestRouterLogsRequestWithoutCredentialsOrQuery(t *testing.T) {
	var logs bytes.Buffer
	handler := NewRouter(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusAccepted) }), slog.New(slog.NewJSONHandler(&logs, nil)), nil)
	request := httptest.NewRequest(http.MethodGet, "/api/v2/jobs?token=signed-url-secret", nil)
	request.Header.Set("Authorization", "Bearer auth-secret")
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusAccepted {
		t.Fatalf("status: %d", response.Code)
	}
	if strings.Contains(logs.String(), "secret") {
		t.Fatal("sensitive data leaked to logs")
	}
	if !strings.Contains(logs.String(), "202") {
		t.Fatal("request status missing")
	}
}

func TestRouterRecoversPanics(t *testing.T) {
	handler := NewRouter(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { panic("secret") }), slog.New(slog.NewJSONHandler(io.Discard, nil)), nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if response.Code != http.StatusInternalServerError {
		t.Fatalf("status: %d", response.Code)
	}
}
