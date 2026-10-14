package workflow

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
)

var ErrInvalid = errors.New("invalid controlled workflow definition")

type Definition struct {
	Template string   `json:"template"`
	Nodes    []string `json:"nodes"`
}
type Version struct {
	ID          string     `json:"id"`
	WorkspaceID *string    `json:"workspace_id"`
	Name        string     `json:"name"`
	Version     int        `json:"version"`
	Status      string     `json:"status"`
	Definition  Definition `json:"definition"`
	ContentHash string     `json:"content_hash"`
	CreatedAt   time.Time  `json:"created_at"`
}
type Repository interface {
	List(context.Context, string) ([]Version, error)
	Get(context.Context, string, string) (Version, error)
	Create(context.Context, identity.Principal, string, Definition, string) (Version, error)
	Publish(context.Context, identity.Principal, string) (Version, error)
	Disable(context.Context, identity.Principal, string) (Version, error)
	Frozen(context.Context, string, string) (Version, error)
}
type Service struct{ repository Repository }

func NewService(repository Repository) *Service { return &Service{repository: repository} }
func Validate(definition Definition) error {
	nodes, err := governance.WorkflowPromptNodes(definition.Template)
	if err != nil || len(nodes) != len(definition.Nodes) {
		return ErrInvalid
	}
	for i, node := range nodes {
		if node != definition.Nodes[i] {
			return ErrInvalid
		}
	}
	return nil
}
func Hash(definition Definition) (string, error) {
	body, err := governance.CanonicalJSON(definition)
	if err != nil {
		return "", err
	}
	return governance.ShaPromptBytes(body), nil
}
func (s *Service) List(ctx context.Context, p identity.Principal) ([]Version, error) {
	return s.repository.List(ctx, p.WorkspaceID)
}
func (s *Service) Get(ctx context.Context, p identity.Principal, id string) (Version, error) {
	return s.repository.Get(ctx, p.WorkspaceID, id)
}
func (s *Service) Create(ctx context.Context, p identity.Principal, name string, definition Definition) (Version, error) {
	if p.Role != "owner" {
		return Version{}, identity.ErrForbidden
	}
	if strings.TrimSpace(name) == "" || len(name) > 120 {
		return Version{}, ErrInvalid
	}
	if err := Validate(definition); err != nil {
		return Version{}, err
	}
	hash, err := Hash(definition)
	if err != nil {
		return Version{}, err
	}
	return s.repository.Create(ctx, p, name, definition, hash)
}
func (s *Service) Publish(ctx context.Context, p identity.Principal, id string) (Version, error) {
	if p.Role != "owner" {
		return Version{}, identity.ErrForbidden
	}
	v, err := s.repository.Get(ctx, p.WorkspaceID, id)
	if err != nil {
		return Version{}, err
	}
	if err = Validate(v.Definition); err != nil {
		return Version{}, err
	}
	hash, err := Hash(v.Definition)
	if err != nil || hash != v.ContentHash {
		return Version{}, ErrInvalid
	}
	return s.repository.Publish(ctx, p, id)
}
func (s *Service) Disable(ctx context.Context, p identity.Principal, id string) (Version, error) {
	if p.Role != "owner" {
		return Version{}, identity.ErrForbidden
	}
	return s.repository.Disable(ctx, p, id)
}
func (s *Service) Frozen(ctx context.Context, workspace, template string) (Version, error) {
	if _, err := governance.WorkflowPromptNodes(template); err != nil {
		return Version{}, ErrInvalid
	}
	v, err := s.repository.Frozen(ctx, workspace, template)
	if err != nil {
		return Version{}, err
	}
	if err = Validate(v.Definition); err != nil {
		return Version{}, err
	}
	hash, err := Hash(v.Definition)
	if err != nil || hash != v.ContentHash || v.Definition.Template != template || v.Status != "published" {
		return Version{}, ErrInvalid
	}
	return v, nil
}

var _ json.RawMessage
