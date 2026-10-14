package resources

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/sakuya/ai-content-platform/apps/control/internal/contracts"
	taskmodule "github.com/sakuya/ai-content-platform/apps/control/internal/modules/task"
)

func (a *ResourceAPI) listJobsHTTP(w http.ResponseWriter, r *http.Request, scope, project string) {
	if r.Method != "GET" {
		methodNotAllowed(w)
		return
	}
	if project == "" {
		project = r.URL.Query().Get("project_id")
		if project == "" {
			project = r.URL.Query().Get("project_id")
		}
	}
	if project != "" {
		if _, err := uuid.Parse(project); err != nil {
			a.resourceError(w, errResourceInvalid)
			return
		}
	}
	limit := 50
	if raw := r.URL.Query().Get("limit"); raw != "" {
		limit, _ = strconv.Atoi(raw)
	}
	if limit < 1 || limit > 100 {
		a.resourceError(w, errResourceInvalid)
		return
	}
	values, err := a.tasks.List(r.Context(), scope, project, limit)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 200, values)
}
func (a *ResourceAPI) jobHTTP(w http.ResponseWriter, r *http.Request, scope string, parts []string) {
	if len(parts) == 0 && r.Method == http.MethodPost {
		var req struct {
			GenerationRequest
			Workflow        string `json:"workflow"`
			WorkflowVersion int    `json:"workflow_version"`
		}
		if !readResourceBody(w, r, &req) {
			return
		}
		if _, err := uuid.Parse(req.ProjectID); err != nil || req.Workflow == "" || strings.Contains(req.Workflow, "@") || req.WorkflowVersion < 1 {
			a.resourceError(w, errResourceInvalid)
			return
		}
		if req.Operation == "" {
			req.Operation = "run"
		}
		if req.Arguments == nil {
			req.Arguments = map[string]any{}
		}
		if req.DocumentType != "" {
			if a.messages == nil {
				a.resourceError(w, errors.New("messaging unavailable"))
				return
			}
			request := req.GenerationRequest
			request.IdempotencyKey = r.Header.Get("Idempotency-Key")
			if request.Operation == "run" {
				request.Operation = ""
			}
			if req.WorkflowVersion != 1 || req.Workflow != selectedWorkflow(request) {
				a.resourceError(w, errResourceInvalid)
				return
			}
			command, err := a.messages.Generate(r.Context(), scope, request)
			if err != nil {
				a.resourceError(w, err)
				return
			}
			writeJSON(w, 202, map[string]any{"job_id": command.JobID, "command_id": command.ID, "project_id": request.ProjectID, "status": "queued"})
			return
		}
		req.Arguments["workflow"], req.Arguments["workflow_version"] = req.Workflow, req.WorkflowVersion
		project, err := a.projects.Get(r.Context(), scope, req.ProjectID)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		a.operationHTTP(w, r, scope, project, req.Operation, req.Arguments)
		return
	}
	if len(parts) == 0 {
		a.listJobsHTTP(w, r, scope, "")
		return
	}
	if _, err := uuid.Parse(parts[0]); err != nil {
		a.resourceError(w, ErrNotFound)
		return
	}
	id := parts[0]
	job, err := a.tasks.Get(r.Context(), scope, id)
	command, failure := job.Command, job.Failure
	if err != nil {
		a.resourceError(w, err)
		return
	}
	if len(parts) == 1 && r.Method == "GET" {
		writeJSON(w, 200, map[string]any{"job_id": job.ID, "status": job.Status, "sequence": job.Sequence, "result": job.Result, "error": failure, "request": json.RawMessage(command.Payload), "project_id": command.ProjectID})
		return
	}
	if len(parts) != 2 {
		http.NotFound(w, r)
		return
	}
	if parts[1] == "resume" && r.Method == "POST" {
		var arguments map[string]any
		if !readResourceBody(w, r, &arguments) {
			return
		}
		if arguments == nil {
			a.resourceError(w, errResourceInvalid)
			return
		}
		action := resourceText(arguments["action"])
		if action != "approve" && action != "revise" && action != "reconcile" {
			a.resourceError(w, errResourceInvalid)
			return
		}
		key := r.Header.Get("Idempotency-Key")
		if len(key) > 200 {
			a.resourceError(w, errResourceInvalid)
			return
		}
		key = fmt.Sprintf("resume:%s:%d:%s", job.ID, job.Sequence, key)
		if len(key) > 200 {
			key = resourceHash([]byte(key))
		}
		command.ID = uuid.NewSHA1(uuid.NameSpaceURL, []byte(scope+":"+key)).String()
		command.Type = "job.resume"
		command.TraceID = command.ID
		command.IdempotencyKey = key
		command.Payload = resourceJSON(arguments)
		if a.messages == nil {
			a.resourceError(w, errors.New("messaging unavailable"))
			return
		}
		err = a.tasks.Control(r.Context(), scope, command)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		writeJSON(w, 202, map[string]any{"job_id": job.ID, "command_id": command.ID, "status": "queued"})
		return
	}
	if parts[1] == "cancel" && r.Method == "POST" {
		key := "cancel:" + job.ID
		cancelID := uuid.NewSHA1(uuid.NameSpaceURL, []byte(scope+":"+key)).String()
		command.ID = cancelID
		command.Type = "job.cancel"
		command.IdempotencyKey = key
		command.TraceID = cancelID
		command.Payload = json.RawMessage(`{}`)
		if a.messages == nil {
			a.resourceError(w, errors.New("messaging unavailable"))
			return
		}
		err := a.tasks.Control(r.Context(), scope, command)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		writeJSON(w, 202, map[string]any{"job_id": job.ID, "status": "cancel_requested"})
		return
	}
	if parts[1] == "events" && r.Method == "GET" {
		a.jobEventsHTTP(w, r, scope, job)
		return
	}
	methodNotAllowed(w)
}
func (a *ResourceAPI) jobEventsHTTP(w http.ResponseWriter, r *http.Request, scope string, job taskmodule.Job) {
	after := int64(0)
	raw := r.URL.Query().Get("after_sequence")
	if raw == "" {
		raw = r.URL.Query().Get("last_event_id")
	}
	if header := r.Header.Get("Last-Event-ID"); header != "" {
		raw = header
	}
	if raw != "" {
		parsed, err := strconv.ParseInt(raw, 10, 64)
		if err == nil {
			after = parsed
		} else {
			after, err = a.tasks.EventSequence(r.Context(), scope, job.ID, raw)
			if err != nil {
				a.resourceError(w, err)
				return
			}
		}
		if after < 0 {
			a.resourceError(w, errResourceInvalid)
			return
		}
	}
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeJSON(w, 500, map[string]string{"error": "streaming unsupported"})
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(200)
	ticker := time.NewTicker(250 * time.Millisecond)
	defer ticker.Stop()
	heartbeat := time.NewTicker(15 * time.Second)
	defer heartbeat.Stop()
	for {
		events, terminal, err := a.readJobEvents(r.Context(), scope, job.ID, after)
		if err != nil {
			if r.Context().Err() == nil {
				_, _ = fmt.Fprint(w, "event: error\ndata: {\"error\":\"event store unavailable\"}\n\n")
				flusher.Flush()
			}
			return
		}
		for _, event := range events {
			if _, err = fmt.Fprintf(w, "id: %d\nevent: %s\ndata: %s\n\n", event.Sequence, event.Type, resourceJSON(event)); err != nil {
				return
			}
			after = event.Sequence
		}
		if len(events) > 0 {
			flusher.Flush()
		}
		if terminal && len(events) < 200 {
			return
		}
		select {
		case <-r.Context().Done():
			return
		case <-heartbeat.C:
			if _, err = fmt.Fprint(w, ": keep-alive\n\n"); err != nil {
				return
			}
			flusher.Flush()
		case <-ticker.C:
		}
	}
}
func (a *ResourceAPI) readJobEvents(ctx context.Context, scope, id string, after int64) ([]contracts.Event, bool, error) {
	return a.tasks.ReadEvents(ctx, scope, id, after)
}
