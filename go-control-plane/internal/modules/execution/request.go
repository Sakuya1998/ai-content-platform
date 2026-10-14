package execution

import "context"

type SnapshotFreezer = func(context.Context, string, map[string]any) (map[string]any, error)

type Request struct {
	IdempotencyKey         string         `json:"idempotency_key"`
	ProjectID              string         `json:"project_id"`
	DocumentID             string         `json:"document_id"`
	DocumentType           string         `json:"document_type"`
	Title                  string         `json:"title"`
	Objective              string         `json:"objective"`
	SourceVersionID        string         `json:"source_version_id,omitempty"`
	InputVersionIDs        []string       `json:"input_version_ids,omitempty"`
	Version                int            `json:"version"`
	Nodes                  []string       `json:"nodes"`
	RequiredMemoryVersions []string       `json:"required_memory_version_ids"`
	Operation              string         `json:"operation,omitempty"`
	RuntimeOperation       string         `json:"runtime_operation,omitempty"`
	Input                  map[string]any `json:"input,omitempty"`
	Brief                  map[string]any `json:"brief,omitempty"`
	BriefVersion           int            `json:"brief_version,omitempty"`
	ContentType            string         `json:"content_type,omitempty"`
	Arguments              map[string]any `json:"arguments,omitempty"`
	ImportedState          map[string]any `json:"imported_state,omitempty"`
	ImportedEnvelope       map[string]any `json:"imported_envelope,omitempty"`
	FrozenResources        map[string]any `json:"frozen_resources,omitempty"`
}
