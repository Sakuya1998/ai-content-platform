package postgres

import (
	"context"
	"errors"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	governancemodels "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance/postgres/models"
	governancepolicies "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance/postgres/policies"
	governanceprompts "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance/postgres/prompts"
	governanceresources "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance/postgres/resources"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type ModelTester = governancemodels.ModelTester
type PolicyRepository = governancepolicies.Repository
type PromptRepository = governanceprompts.Repository
type AuditWriter func(context.Context, pgx.Tx, identity.Principal, string, string, string) error

type GovernanceRepository struct {
	pool             *pgxpool.Pool
	modelCatalog     *governancemodels.Repository
	resourceCatalog  *governanceresources.Repository
	promptCatalog    *PromptRepository
	creationPolicies *PolicyRepository
	audit            AuditWriter
}

func (g *GovernanceRepository) SetPromptCatalog(prompts *PromptRepository) { g.promptCatalog = prompts }
func (g *GovernanceRepository) SetCreationPolicies(policies *PolicyRepository) {
	g.creationPolicies = policies
}

func NewRepositories(pool *pgxpool.Pool, encryptionKey string, tester ModelTester, audit ...AuditWriter) (*GovernanceRepository, *PromptRepository, *PolicyRepository) {
	var writer AuditWriter
	if len(audit) > 0 {
		writer = audit[0]
	}
	repo := &GovernanceRepository{pool: pool, audit: writer}
	prompts := governanceprompts.NewRepository(pool, repo.writeAudit)
	policies := governancepolicies.NewRepository(pool, repo.writeAudit)
	repo.promptCatalog = prompts
	repo.creationPolicies = policies
	repo.modelCatalog = governancemodels.NewRepository(pool, encryptionKey, tester, repo.writeAudit)
	repo.resourceCatalog = governanceresources.NewRepository(
		pool,
		repo.writeAudit,
		normalizeGovernancePayload,
		builtinGovernanceStyle,
		regexpResourceKey,
		builtinStyleKeys(),
	)
	return repo, prompts, policies
}

func (g *GovernanceRepository) writeAudit(ctx context.Context, tx pgx.Tx, principal identity.Principal, action, resourceType, resourceID string) error {
	if g.audit == nil {
		return errors.New("audit writer unavailable")
	}
	return g.audit(ctx, tx, principal, action, resourceType, resourceID)
}

func (g *GovernanceRepository) EncryptSecret(secret string) ([]byte, error) {
	return g.encryptSecret(secret)
}
func (g *GovernanceRepository) DecryptSecret(secret []byte) (string, error) {
	return g.decryptSecret(secret)
}
func (g *GovernanceRepository) encryptSecret(secret string) ([]byte, error) {
	return g.modelCatalog.EncryptSecret(secret)
}
func (g *GovernanceRepository) decryptSecret(encrypted []byte) (string, error) {
	return g.modelCatalog.DecryptSecret(encrypted)
}
func (g *GovernanceRepository) ExecuteResource(ctx context.Context, p identity.Principal, kind string, command governance.CatalogCommand) (governance.CatalogResult, error) {
	if g.resourceCatalog == nil {
		return governance.CatalogResult{}, errors.New("resource repository unavailable")
	}
	status, result, err := g.resourceCatalog.ExecuteResourceCatalogOp(ctx, p, kind, command.Path, command.Action, command.Filters, command.Payload)
	return catalogRepositoryResult(status, result, err), nil
}

func catalogRepositoryResult(status int, result any, err error) governance.CatalogResult {
	out := &catalogResult{}
	if err != nil {
		if status >= 400 && status < 500 || status == 503 {
			catalogReject(out, status, err.Error())
		} else {
			catalogDBError(out, err)
		}
		return out.result
	}
	catalogComplete(out, status, result)
	return out.result
}
func (g *GovernanceRepository) ExecuteModel(ctx context.Context, p identity.Principal, command governance.CatalogCommand) (governance.CatalogResult, error) {
	if g.modelCatalog == nil {
		return governance.CatalogResult{}, errors.New("model repository unavailable")
	}
	status, result, err := g.modelCatalog.ExecuteModelCatalogOp(ctx, p, command.Path, command.Action, command.Payload)
	return catalogRepositoryResult(status, result, err), nil
}
