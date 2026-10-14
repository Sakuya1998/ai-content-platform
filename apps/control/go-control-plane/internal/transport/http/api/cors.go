package api

import (
	"net/http"
	"strings"
)

func CredentialedCORS(next http.Handler, origins []string) http.Handler {
	allowed := map[string]bool{}
	for _, origin := range origins {
		if origin != "*" && origin != "" {
			allowed[origin] = true
		}
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if allowed[origin] {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Access-Control-Allow-Credentials", "true")
			w.Header().Add("Vary", "Origin")
		}
		if r.Method == http.MethodOptions && r.Header.Get("Access-Control-Request-Method") != "" {
			if !allowed[origin] {
				writeJSON(w, 403, map[string]string{"error": "origin not allowed"})
				return
			}
			method := r.Header.Get("Access-Control-Request-Method")
			if !strings.Contains(",GET,POST,PUT,PATCH,DELETE,HEAD,", ","+method+",") {
				writeJSON(w, 403, map[string]string{"error": "method not allowed"})
				return
			}
			allowedHeaders := map[string]bool{"content-type": true, "x-csrf-token": true, "x-workspace-id": true, "idempotency-key": true, "last-event-id": true, "authorization": true, "if-match": true}
			for _, header := range strings.Split(r.Header.Get("Access-Control-Request-Headers"), ",") {
				if strings.TrimSpace(header) != "" && !allowedHeaders[strings.ToLower(strings.TrimSpace(header))] {
					writeJSON(w, 403, map[string]string{"error": "header not allowed"})
					return
				}
			}
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, HEAD")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type, X-CSRF-Token, X-Workspace-ID, Idempotency-Key, Last-Event-ID, Authorization, If-Match")
			w.Header().Set("Access-Control-Expose-Headers", "ETag, Content-Disposition")
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}
