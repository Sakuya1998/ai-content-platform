package response

import (
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/project"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/task"
	"io"
	"net/http"
	"strings"
)

func CanonicalPath(path string) string {
	for _, prefix := range []string{"/api/v2/", "/api/v1/"} {
		if strings.HasPrefix(path, prefix) {
			return "/api/" + strings.TrimPrefix(path, prefix)
		}
	}
	return strings.TrimSuffix(path, "/")
}

func ResourceError(w http.ResponseWriter, err error) {
	status, message := 503, "resource store unavailable"
	var databaseError *pgconn.PgError
	if errors.Is(err, pgx.ErrNoRows) || errors.Is(err, project.ErrNotFound) {
		status, message = 404, "resource not found"
	} else if errors.Is(err, task.ErrConflict) || errors.As(err, &databaseError) && databaseError.Code == "23505" {
		status, message = 409, "resource version or active job conflict"
	}
	JSON(w, status, map[string]string{"error": message})
}

func JSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func MethodNotAllowed(w http.ResponseWriter) {
	w.Header().Set("Allow", "GET, POST, PUT, DELETE")
	JSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
}

func DecodeJSON(w http.ResponseWriter, r *http.Request, value any) bool {
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if decoder.Decode(value) != nil {
		JSON(w, 400, map[string]string{"error": "invalid request body"})
		return false
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		JSON(w, 400, map[string]string{"error": "one JSON object required"})
		return false
	}
	return true
}
