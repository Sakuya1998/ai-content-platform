// Package project owns content-independent project definitions.
package project

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"
)

type Project struct {
	ID          string    `json:"id"`
	WorkspaceID string    `json:"workspace_id"`
	ProjectType string    `json:"content_type"`
	Name        string    `json:"name"`
	Description string    `json:"description,omitempty"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

type Repository interface {
	Create(context.Context, string, map[string]any, string) (Resource, error)
}

type Service struct{ Repository Repository }

func (s Service) Create(ctx context.Context, scope string, input map[string]any, key string) (Resource, error) {
	if err := Normalize(input); err != nil {
		return Resource{}, err
	}
	if len(key) > 200 {
		return Resource{}, ErrInvalid
	}
	return s.Repository.Create(ctx, scope, input, key)
}

var ErrInvalid = errors.New("invalid project definition")
var ErrNotFound = errors.New("project not found")

type Resource struct {
	ID           string         `json:"id"`
	WorkspaceID  string         `json:"workspace_id"`
	DocumentID   string         `json:"document_id"`
	Title        string         `json:"title"`
	ContentType  string         `json:"content_type"`
	Input        map[string]any `json:"input"`
	State        map[string]any `json:"projection"`
	StateHash    string         `json:"projection_hash"`
	BriefVersion int            `json:"brief_version"`
	Revision     int64          `json:"revision"`
}

func (r Resource) MarshalJSON() ([]byte, error) {
	type base Resource
	return json.Marshal(struct {
		base
		Input any `json:"input"`
		Brief any `json:"brief"`
	}{base: base(r), Input: r.Input["input"], Brief: r.Input["brief"]})
}

func (r *Resource) UnmarshalJSON(body []byte) error {
	type base Resource
	var wire struct {
		base
		Input map[string]any `json:"input"`
		Brief map[string]any `json:"brief"`
	}
	if err := json.Unmarshal(body, &wire); err != nil {
		return err
	}
	*r = Resource(wire.base)
	r.Input = map[string]any{"title": r.Title, "content_type": r.ContentType, "input": wire.Input, "brief": wire.Brief}
	return nil
}

// Normalize validates the common contract; format-specific inputs remain opaque.
func Normalize(input map[string]any) error {
	if input == nil {
		return ErrInvalid
	}
	title, _ := input["title"].(string)
	if title == "" {
		title, _ = input["name"].(string)
	}
	title = strings.TrimSpace(title)
	kind, _ := input["content_type"].(string)
	kind = strings.TrimSpace(kind)
	if title == "" || len([]rune(title)) > 200 || kind == "" || len(kind) > 120 {
		return ErrInvalid
	}
	definition, ok := input["input"].(map[string]any)
	if !ok {
		definition = map[string]any{}
	}
	brief, exists := input["brief"]
	if exists {
		if _, ok := brief.(map[string]any); !ok {
			return ErrInvalid
		}
	} else {
		input["brief"] = map[string]any{}
	}
	input["title"], input["content_type"], input["input"] = title, kind, definition
	return nil
}

func Workflow(kind string) string {
	if kind == "novel" {
		return "novel.write@1"
	}
	return "content.generate@1"
}
