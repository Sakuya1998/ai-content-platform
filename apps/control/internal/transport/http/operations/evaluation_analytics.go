package operations

import (
	"net/http"
	"strconv"
	"time"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/evaluation"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
)

func (a *OperationsAPI) evaluationAnalytics(w http.ResponseWriter, r *http.Request, principal identity.Principal, path string) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	if a.evaluations == nil {
		operationError(w, evaluation.ErrUnavailable)
		return
	}
	switch path {
	case "/api/evaluations/stats":
		var since time.Time
		if raw := r.URL.Query().Get("since"); raw != "" {
			parsed, err := time.Parse(time.RFC3339, raw)
			if err != nil {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "since must be an RFC3339 timestamp"})
				return
			}
			since = parsed
		}
		stats, err := a.evaluations.GetWorkspaceStats(r.Context(), principal.WorkspaceID, since)
		if err != nil {
			operationError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, stats)
	case "/api/evaluations/trends":
		dimension := r.URL.Query().Get("dimension")
		limit := 100
		if raw := r.URL.Query().Get("limit"); raw != "" {
			parsed, err := strconv.Atoi(raw)
			if err != nil || parsed < 1 || parsed > 500 {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "limit must be between 1 and 500"})
				return
			}
			limit = parsed
		}
		if dimension == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "dimension is required"})
			return
		}
		points, err := a.evaluations.GetDimensionTrends(r.Context(), principal.WorkspaceID, dimension, limit)
		if err != nil {
			operationError(w, err)
			return
		}
		reports := make([]*evaluation.EvaluationReport, 0, len(points))
		for _, point := range points {
			reports = append(reports, &evaluation.EvaluationReport{CreatedAt: point.CreatedAt, OverallScore: point.Score})
		}
		writeJSON(w, http.StatusOK, map[string]any{
			"dimension": dimension, "items": points, "trend": evaluation.CalculateTrend(reports),
		})
	case "/api/evaluations/dimensions":
		items, err := a.evaluations.ListDimensions(r.Context(), r.URL.Query().Get("category"))
		if err != nil {
			operationError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"items": items})
	case "/api/evaluations/issues":
		limit := 20
		if raw := r.URL.Query().Get("limit"); raw != "" {
			parsed, err := strconv.Atoi(raw)
			if err != nil || parsed < 1 || parsed > 100 {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "limit must be between 1 and 100"})
				return
			}
			limit = parsed
		}
		items, err := a.evaluations.GetTopIssues(r.Context(), principal.WorkspaceID, limit)
		if err != nil {
			operationError(w, err)
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"items": items})
	}
}
