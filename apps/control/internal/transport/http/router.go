package http

import (
	"log/slog"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/api"
)

func NewRouter(handler http.Handler, logger *slog.Logger, allowedOrigins []string) http.Handler {
	router := chi.NewRouter()
	router.Use(middleware.RequestID)
	router.Use(func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			started := time.Now()
			response := middleware.NewWrapResponseWriter(w, r.ProtoMajor)
			defer func() {
				if recover() != nil {
					logger.ErrorContext(r.Context(), "HTTP handler panicked", "request_id", middleware.GetReqID(r.Context()))
					if response.Status() == 0 {
						http.Error(response, "internal server error", http.StatusInternalServerError)
					}
				}
				status := response.Status()
				if status == 0 {
					status = http.StatusOK
				}
				// URLs and headers may contain credentials or signed object references.
				logger.InfoContext(r.Context(), "HTTP request", "method", r.Method, "status", status,
					"duration_ms", time.Since(started).Milliseconds(), "request_id", middleware.GetReqID(r.Context()))
			}()
			next.ServeHTTP(response, r)
		})
	})
	router.Mount("/", handler)
	return api.CredentialedCORS(router, allowedOrigins)
}
