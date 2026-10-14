package governancehttp

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/governance"
	governancepostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/governance/postgres"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/identity"
	identitypostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/identity/postgres"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/modelprovider"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/password"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/security"
	identityhttp "github.com/sakuya/novel-agent/apps/control/internal/transport/http/identity"
)

type IdentityAPI = identityhttp.Handler
type IdentitySettings = identityhttp.Settings
type Principal = identity.Principal

func PrincipalFromContext(ctx context.Context) (Principal, bool) { return identity.FromContext(ctx) }
func WithPrincipal(ctx context.Context, p Principal) context.Context {
	return identity.WithPrincipal(ctx, p)
}
func NewIdentityAPI(pool *pgxpool.Pool, settings IdentitySettings) *IdentityAPI {
	return identityhttp.NewHandler(identity.NewService(identitypostgres.NewRepository(pool), password.Codec{}, settings.SessionHours), settings)
}

const identityCookie = identityhttp.CookieName

func identityCSRF(token string) string       { return identityhttp.CSRF(token) }
func identityCheckCSRF(r *http.Request) bool { return identityhttp.CheckCSRF(r) }

var identityUsername = regexp.MustCompile(`^[A-Za-z0-9_.-]{3,80}$`)

func canonicalPublicPath(path string) string {
	for _, prefix := range []string{"/api/v2/", "/api/v1/"} {
		if strings.HasPrefix(path, prefix) {
			return "/api/" + strings.TrimPrefix(path, prefix)
		}
	}
	return strings.TrimSuffix(path, "/")
}
func identityError(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}
func governanceDBError(w http.ResponseWriter, err error) {
	var e *pgconn.PgError
	if errors.Is(err, pgx.ErrNoRows) {
		identityError(w, 404, "resource not found")
	} else if errors.As(err, &e) && (e.Code == "23505" || e.Code == "23503") {
		identityError(w, 409, "resource conflict")
	} else {
		identityError(w, 503, "storage unavailable")
	}
}
func workspaceTx(ctx context.Context, pool *pgxpool.Pool, workspace string) (pgx.Tx, error) {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, "SELECT set_config('app.workspace_id',$1,true)", workspace); err != nil {
		_ = tx.Rollback(ctx)
		return nil, err
	}
	return tx, nil
}
func appendIdentityAudit(ctx context.Context, tx pgx.Tx, p Principal, action, kind, id string) error {
	return identitypostgres.RecordPrincipalAudit(ctx, tx, p, action, kind, id)
}

type GovernanceAPI struct {
	*governancepostgres.GovernanceRepository
	handler  http.Handler
	prompts  *governancepostgres.PromptRepository
	policies *governancepostgres.PolicyRepository
	cipher   *security.ExecutionCipher
}
type PromptAPI struct {
	*governancepostgres.PromptRepository
	handler http.Handler
}
type CreationPolicyAPI struct {
	*governancepostgres.PolicyRepository
	handler http.Handler
}

func NewGovernanceAPI(pool *pgxpool.Pool, auth *IdentityAPI, key, token string) *GovernanceAPI {
	var authenticator Authenticator
	if auth != nil {
		authenticator = auth
	}
	repo, prompts, policies := governancepostgres.NewRepositories(pool, key, modelprovider.NewModelTester(), identitypostgres.RecordPrincipalAudit)
	service := governance.NewCatalogService(repo, prompts, policies)
	return &GovernanceAPI{GovernanceRepository: repo, prompts: prompts, policies: policies, handler: NewHandler(service, authenticator, "resources")}
}
func NewPromptAPI(pool *pgxpool.Pool, auth *IdentityAPI, token string) *PromptAPI {
	var authenticator Authenticator
	if auth != nil {
		authenticator = auth
	}
	repo, prompts, policies := governancepostgres.NewRepositories(pool, "", nil, identitypostgres.RecordPrincipalAudit)
	return &PromptAPI{PromptRepository: prompts, handler: NewHandler(governance.NewCatalogService(repo, prompts, policies), authenticator, "prompts")}
}
func NewCreationPolicyAPI(pool *pgxpool.Pool, auth *IdentityAPI, token string) *CreationPolicyAPI {
	var authenticator Authenticator
	if auth != nil {
		authenticator = auth
	}
	repo, prompts, policies := governancepostgres.NewRepositories(pool, "", nil, identitypostgres.RecordPrincipalAudit)
	return &CreationPolicyAPI{PolicyRepository: policies, handler: NewHandler(governance.NewCatalogService(repo, prompts, policies), authenticator, "policies")}
}
func (g *GovernanceAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) { g.handler.ServeHTTP(w, r) }
func (g *PromptAPI) ServeHTTP(w http.ResponseWriter, r *http.Request)     { g.handler.ServeHTTP(w, r) }
func (g *CreationPolicyAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	g.handler.ServeHTTP(w, r)
}
func (g *GovernanceAPI) SetPromptCatalog(catalog *PromptAPI) {
	g.prompts = catalog.PromptRepository
	g.GovernanceRepository.SetPromptCatalog(g.prompts)
}
func (g *GovernanceAPI) SetCreationPolicies(catalog *CreationPolicyAPI) {
	g.policies = catalog.PolicyRepository
	g.GovernanceRepository.SetCreationPolicies(g.policies)
}
func (g *GovernanceAPI) SetExecutionEncryptionKey(value string) error {
	cipher, err := security.NewExecutionCipher(value)
	if err == nil {
		g.cipher = cipher
	}
	return err
}
func (g *GovernanceAPI) ResolveExecutionResources(ctx context.Context, workspace string, snapshot map[string]any) (map[string]any, error) {
	return governance.NewExecutionService(g.GovernanceRepository, g.cipher).Resolve(ctx, workspace, snapshot)
}
func (g *GovernanceAPI) executionRouteSnapshot(route FrozenRoute, credentials map[string]string) (map[string]any, error) {
	return governance.NewExecutionService(nil, g.cipher).RouteSnapshot(route, credentials)
}
func (g *GovernanceAPI) encryptSecret(secret string) ([]byte, error) { return g.EncryptSecret(secret) }
func (g *GovernanceAPI) decryptSecret(secret []byte) (string, error) { return g.DecryptSecret(secret) }
func (g *GovernanceAPI) encryptExecutionCredential(secret string) (string, error) {
	if g.cipher == nil {
		cipher, _ := security.NewExecutionCipher("")
		return cipher.Encrypt(secret)
	}
	return g.cipher.Encrypt(secret)
}
func (g *GovernanceAPI) decryptExecutionCredential(secret string) (string, error) {
	if g.cipher == nil {
		cipher, _ := security.NewExecutionCipher("")
		return cipher.Decrypt(secret)
	}
	return g.cipher.Decrypt(secret)
}
func (g *GovernanceAPI) testModelProfile(w http.ResponseWriter, r *http.Request, profile ModelProfile) {
	var input governance.ModelTest
	if !decodeJSON(w, r, &input) {
		return
	}
	secret, err := g.DecryptSecret(profile.EncryptedKey)
	if err != nil {
		identityError(w, 503, "model key unavailable")
		return
	}
	profile.APIKey = secret
	result, err := modelprovider.NewModelTester().Test(r.Context(), profile, input)
	if err != nil {
		identityError(w, 422, err.Error())
		return
	}
	writeJSON(w, 200, result)
}

type ResourceVersion = governance.ResourceVersion
type ModelProfile = governance.ModelProfile
type RouteTarget = governance.RouteTarget
type FrozenRoute = governance.FrozenRoute
type FrozenRoutes = governance.FrozenRoutes
type PromptMessage = governance.PromptMessage
type PromptVersion = governance.PromptVersion
type PromptPackEntry = governance.PromptPackEntry
type PromptPackVersion = governance.PromptPackVersion
type FrozenPromptBinding = governance.FrozenPromptBinding
type CreationPolicyVersion = governance.CreationPolicyVersion
type modelProfileWrite = governance.ModelProfileWrite

var normalizeGovernanceKind = governance.NormalizeKind
var validateModelProfile = governance.ValidateModelProfile
var providerURL = governance.ProviderURL
var modelContains = governance.ModelContains
var builtinGovernanceStyle = governancepostgres.BuiltinGovernanceStyle
var workflowPromptNodes = governance.WorkflowPromptNodes
var promptHash = governance.PromptHash
var verifyCreationPolicy = governance.VerifyCreationPolicy
var validateCreationPolicy = governance.ValidateCreationPolicy
var validatePromptContract = governance.ValidatePromptContract
var renderPromptPreview = governance.RenderPromptPreview
var creationPolicyHash = governance.CreationPolicyHash

func canonicalPromptJSON(value any) ([]byte, error) { return governance.CanonicalJSON(value) }

var _ json.RawMessage

var normalizeGovernancePayload = governance.NormalizePayload
var compilePromptSchema = governance.CompilePromptSchema

func decodeJSON(w http.ResponseWriter, r *http.Request, value any) bool {
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if decoder.Decode(value) != nil {
		reject(w, 400, "invalid request body")
		return false
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		reject(w, 400, "invalid request body")
		return false
	}
	return true
}
