package governance

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/sakuya/novel-agent/apps/control/internal/modules/identity"
)

type Action string

const (
	Read    Action = "read"
	Create  Action = "create"
	Replace Action = "replace"
	Update  Action = "update"
	Delete  Action = "delete"
)

type CatalogCommand struct {
	Action  Action
	Path    []string
	Payload json.RawMessage
	Filters map[string]string
}
type CatalogResult struct {
	Kind  string
	Value any
}
type ResourceRepository interface {
	ExecuteResource(context.Context, identity.Principal, string, CatalogCommand) (CatalogResult, error)
	ExecuteModel(context.Context, identity.Principal, CatalogCommand) (CatalogResult, error)
}
type PromptRepository interface {
	ExecutePrompt(context.Context, identity.Principal, string, CatalogCommand) (CatalogResult, error)
}
type PolicyRepository interface {
	ExecutePolicy(context.Context, identity.Principal, string, CatalogCommand) (CatalogResult, error)
}
type CatalogService struct {
	resources ResourceRepository
	prompts   PromptRepository
	policies  PolicyRepository
}

func NewCatalogService(resources ResourceRepository, prompts PromptRepository, policies PolicyRepository) *CatalogService {
	return &CatalogService{resources: resources, prompts: prompts, policies: policies}
}
func authorizeCatalog(p identity.Principal, command CatalogCommand) error {
	if p.UserID == "" || p.WorkspaceID == "" {
		return identity.ErrUnauthorized
	}
	if command.Action != Read && p.Role != "owner" {
		return identity.ErrForbidden
	}
	return nil
}
func (s *CatalogService) Resource(ctx context.Context, p identity.Principal, kind string, command CatalogCommand) (CatalogResult, error) {
	if err := authorizeCatalog(p, command); err != nil {
		return CatalogResult{}, err
	}
	if s.resources == nil {
		return CatalogResult{}, errors.New("governance repository unavailable")
	}
	return s.resources.ExecuteResource(ctx, p, kind, command)
}
func (s *CatalogService) Model(ctx context.Context, p identity.Principal, command CatalogCommand) (CatalogResult, error) {
	if err := authorizeCatalog(p, command); err != nil {
		return CatalogResult{}, err
	}
	if s.resources == nil {
		return CatalogResult{}, errors.New("model repository unavailable")
	}
	return s.resources.ExecuteModel(ctx, p, command)
}
func (s *CatalogService) Prompt(ctx context.Context, p identity.Principal, kind string, command CatalogCommand) (CatalogResult, error) {
	if err := authorizeCatalog(p, command); err != nil {
		return CatalogResult{}, err
	}
	if s.prompts == nil {
		return CatalogResult{}, errors.New("prompt repository unavailable")
	}
	return s.prompts.ExecutePrompt(ctx, p, kind, command)
}
func (s *CatalogService) Policy(ctx context.Context, p identity.Principal, kind string, command CatalogCommand) (CatalogResult, error) {
	if err := authorizeCatalog(p, command); err != nil {
		return CatalogResult{}, err
	}
	if s.policies == nil {
		return CatalogResult{}, errors.New("creation policy repository unavailable")
	}
	return s.policies.ExecutePolicy(ctx, p, kind, command)
}
