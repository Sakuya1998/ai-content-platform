package resources

import (
	"encoding/json"
	"github.com/google/uuid"
	contentmodule "github.com/sakuya/novel-agent/go-control-plane/internal/modules/content"
	"net/http"
	"strconv"
	"strings"
)

func (a *ResourceAPI) contentHTTP(w http.ResponseWriter, r *http.Request, scope string, parts []string) {
	if r.Method == http.MethodPost {
		a.writeContentHTTP(w, r, scope, parts)
		return
	}
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	if len(parts) == 1 || len(parts) == 2 && parts[1] == "documents" {
		project := r.URL.Query().Get("project_id")
		if project != "" {
			if _, err := uuid.Parse(project); err != nil {
				a.resourceError(w, errResourceInvalid)
				return
			}
		}
		values, err := a.content.Documents(r.Context(), scope, project)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		writeJSON(w, 200, values)
		return
	}
	if len(parts) == 3 && parts[0] == "content" && parts[1] == "versions" {
		if _, err := uuid.Parse(parts[2]); err != nil {
			a.resourceError(w, ErrNotFound)
			return
		}
		project := r.URL.Query().Get("project_id")
		if project != "" {
			if _, err := uuid.Parse(project); err != nil {
				a.resourceError(w, ErrNotFound)
				return
			}
		}
		body, err := a.content.VersionByID(r.Context(), scope, parts[2], project)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		writeJSON(w, 200, body)
		return
	}
	offset := 1
	if parts[0] == "content" && len(parts) > 1 && parts[1] == "documents" {
		offset = 2
	}
	if len(parts) < offset+1 {
		http.NotFound(w, r)
		return
	}
	document := parts[offset]
	if _, err := uuid.Parse(document); err != nil {
		a.resourceError(w, ErrNotFound)
		return
	}
	if len(parts) == offset+1 {
		value, err := a.content.Document(r.Context(), scope, document)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		writeJSON(w, 200, value)
		return
	}
	if parts[offset+1] != "versions" {
		http.NotFound(w, r)
		return
	}
	if len(parts) == offset+2 {
		values, err := a.content.Versions(r.Context(), scope, document)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		writeJSON(w, 200, values)
		return
	}
	if len(parts) != offset+3 {
		http.NotFound(w, r)
		return
	}
	if parts[offset+2] == "diff" {
		from, e1 := strconv.Atoi(r.URL.Query().Get("from_version"))
		to, e2 := strconv.Atoi(r.URL.Query().Get("to_version"))
		if e1 != nil || e2 != nil || from < 1 || to < 1 {
			a.resourceError(w, errResourceInvalid)
			return
		}
		before, err := a.content.Version(r.Context(), scope, document, from)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		after, err := a.content.Version(r.Context(), scope, document, to)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		var first, second any
		_ = json.Unmarshal(before, &first)
		_ = json.Unmarshal(after, &second)
		aText, _ := json.MarshalIndent(first, "", "  ")
		bText, _ := json.MarshalIndent(second, "", "  ")
		writeJSON(w, 200, map[string]any{"from_version": from, "to_version": to, "diff": resourceUnifiedDiff(string(aText), string(bText), from, to)})
		return
	}
	version, err := strconv.Atoi(strings.TrimSpace(parts[offset+2]))
	if err != nil || version < 1 {
		a.resourceError(w, ErrNotFound)
		return
	}
	body, err := a.content.Version(r.Context(), scope, document, version)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 200, body)
}
func (a *ResourceAPI) writeContentHTTP(w http.ResponseWriter, r *http.Request, scope string, parts []string) {
	if len(parts) == 1 && parts[0] == "documents" {
		var req struct {
			ProjectID    string `json:"project_id"`
			Title        string `json:"title"`
			DocumentType string `json:"document_type"`
		}
		if !readResourceBody(w, r, &req) {
			return
		}
		if _, err := uuid.Parse(req.ProjectID); err != nil || strings.TrimSpace(req.Title) == "" || strings.TrimSpace(req.DocumentType) == "" {
			a.resourceError(w, errResourceInvalid)
			return
		}
		document, err := a.content.CreateDocument(r.Context(), scope, req.ProjectID, req.DocumentType, req.Title)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		writeJSON(w, 201, document)
		return
	}
	if len(parts) == 3 && parts[0] == "documents" && parts[2] == "versions" {
		if _, err := uuid.Parse(parts[1]); err != nil {
			a.resourceError(w, ErrNotFound)
			return
		}
		var req struct {
			Expected int             `json:"expected_version"`
			Document json.RawMessage `json:"document"`
		}
		if !readResourceBody(w, r, &req) {
			return
		}
		version, err := (contentmodule.Service{Repository: a.content}).Append(r.Context(), scope, parts[1], req.Expected, req.Document)
		if err != nil {
			if err == contentmodule.ErrInvalid {
				err = errResourceInvalid
			}
			a.resourceError(w, err)
			return
		}
		writeJSON(w, 201, version)
		return
	}
	http.NotFound(w, r)
}
