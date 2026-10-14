package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestCredentialedCORSPreflightAndActualResponse(t *testing.T) {
	called := false
	handler := CredentialedCORS(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { called = true; w.WriteHeader(200) }), []string{"http://localhost:5173"})
	r := httptest.NewRequest("OPTIONS", "/api/auth/login", nil)
	r.Header.Set("Origin", "http://localhost:5173")
	r.Header.Set("Access-Control-Request-Method", "POST")
	r.Header.Set("Access-Control-Request-Headers", "Content-Type,X-CSRF-Token")
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	if w.Code != 204 || called || w.Header().Get("Access-Control-Allow-Credentials") != "true" {
		t.Fatal("CORS preflight failed")
	}
	r = httptest.NewRequest("GET", "/api/auth/me", nil)
	r.Header.Set("Origin", "http://localhost:5173")
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	if !called || w.Header().Get("Access-Control-Allow-Origin") != "http://localhost:5173" {
		t.Fatal("actual CORS response failed")
	}
	r = httptest.NewRequest("OPTIONS", "/api/auth/login", nil)
	r.Header.Set("Origin", "https://bad.example")
	r.Header.Set("Access-Control-Request-Method", "POST")
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	if w.Code != 403 || w.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Fatal("untrusted origin allowed")
	}
}
