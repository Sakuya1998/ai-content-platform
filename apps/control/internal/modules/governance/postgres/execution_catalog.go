package postgres

import (
	"context"

	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/governance"
)

func (g *GovernanceRepository) Prompt(ctx context.Context, workspace, id string) (governance.PromptVersion, error) {
	return g.promptCatalog.FrozenVersion(ctx, workspace, id)
}
func (g *GovernanceRepository) Policy(ctx context.Context, workspace, id string) (governance.CreationPolicyVersion, error) {
	return g.creationPolicies.FrozenVersion(ctx, workspace, id)
}
func (g *GovernanceRepository) Routes(ctx context.Context, workspace, id string) (governance.FrozenRoutes, error) {
	return g.ResolveFrozenRoutes(ctx, workspace, id)
}
func (g *GovernanceRepository) Resource(ctx context.Context, workspace, id string) (governance.ResourceVersion, error) {
	return g.FrozenResource(ctx, workspace, id)
}

var BuiltinGovernanceStyle = builtinGovernanceStyle
