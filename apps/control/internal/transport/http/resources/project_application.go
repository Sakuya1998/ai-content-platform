package resources

import (
	"net/http"
)

func (a *ResourceAPI) updateResourceHTTP(w http.ResponseWriter, r *http.Request, scope string, current ProjectResource) {
	var req struct {
		Title    string         `json:"title"`
		Input    map[string]any `json:"input"`
		Expected int64          `json:"expected_revision"`
	}
	if !readResourceBody(w, r, &req) {
		return
	}
	input := map[string]any{"title": req.Title, "content_type": current.ContentType, "input": req.Input, "brief": current.Input["brief"]}
	if err := normalizeResourceInput(input); err != nil {
		a.resourceError(w, err)
		return
	}
	current, err := a.projects.Update(r.Context(), scope, current.ID, req.Expected, input)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 200, current)
}
