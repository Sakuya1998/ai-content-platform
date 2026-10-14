package postgres

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	contentpostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/content/postgres"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/governance"
	governancepostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/governance/postgres"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/identity"
	identitypostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/identity/postgres"
	projectmodule "github.com/sakuya/ai-content-platform/apps/control/internal/modules/project"
	projectpostgres "github.com/sakuya/ai-content-platform/apps/control/internal/modules/project/postgres"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/task"
	"github.com/sakuya/ai-content-platform/apps/control/internal/platform/config"
	"github.com/sakuya/ai-content-platform/apps/control/internal/platform/password"
	governancehttp "github.com/sakuya/ai-content-platform/apps/control/internal/transport/http/governance"
	identityhttp "github.com/sakuya/ai-content-platform/apps/control/internal/transport/http/identity"
)

type modelProfileWrite = governance.ModelProfileWrite
type ImageModel = governance.ImageModel
type ImageCapabilities = governance.ImageCapabilities
type SpeechModel = governance.SpeechModel
type SpeechCapabilities = governance.SpeechCapabilities
type MusicModel = governance.MusicModel
type MusicCapabilities = governance.MusicCapabilities
type SoundEffectModel = governance.SoundEffectModel
type SoundEffectCapabilities = governance.SoundEffectCapabilities
type VideoModel = governance.VideoModel

var validateModelProfile = governance.ValidateModelProfile
var providerURL = governance.ProviderURL
var workflowPromptNodes = governance.WorkflowPromptNodes
var canonicalPromptJSON = governance.CanonicalJSON

type IdentityAPI = identityhttp.Handler
type IdentitySettings = identityhttp.Settings
type Principal = identity.Principal

var WithPrincipal = identity.WithPrincipal

func NewIdentityAPI(pool *pgxpool.Pool, settings IdentitySettings) *IdentityAPI {
	return identityhttp.NewHandler(identity.NewService(identitypostgres.NewRepository(pool), password.Codec{}, settings.SessionHours), settings)
}

func createProjectFixture(pool *pgxpool.Pool, input projectmodule.Project) (projectmodule.Project, error) {
	resource, err := (projectpostgres.Repository{Pool: pool, Conflict: task.ErrConflict, MarkCandidatesStale: MarkCandidatesStale, CreateInitialDocument: contentpostgres.CreateDocumentWithin}).Create(
		context.Background(), input.WorkspaceID,
		map[string]any{"title": input.Name, "content_type": input.ProjectType, "input": map[string]any{}, "brief": map[string]any{}},
		uuid.NewString(),
	)
	if err != nil {
		return projectmodule.Project{}, err
	}
	return projectmodule.Project{ID: resource.ID, WorkspaceID: resource.WorkspaceID, ProjectType: resource.ContentType, Name: resource.Title}, nil
}

func newTestMQRepository(pool *pgxpool.Pool, topics config.MQTopics) *MQRepository {
	return NewMQRepository(pool, topics, RepositorySettings{
		PrepareContentDocument: contentpostgres.PrepareGeneratedDocument,
		CommitContentVersion:   contentpostgres.CommitGeneratedVersion,
		ReadContentVersion:     contentpostgres.ReadVersionWithin,
		VersionIDBySourceJob:   contentpostgres.VersionIDBySourceJob,
		FrozenModelProfile:     governancepostgres.FrozenModelProfile,
	})
}

type GovernanceAPI struct {
	*governancepostgres.GovernanceRepository
	handler http.Handler
}
type PromptAPI struct {
	*governancepostgres.PromptRepository
}
type CreationPolicyAPI struct {
	*governancepostgres.PolicyRepository
}

func NewGovernanceAPI(pool *pgxpool.Pool, auth *IdentityAPI, key, token string) *GovernanceAPI {
	repository, prompts, policies := governancepostgres.NewRepositories(pool, key, nil, identitypostgres.RecordPrincipalAudit)
	return &GovernanceAPI{GovernanceRepository: repository, handler: governancehttp.NewHandler(governance.NewCatalogService(repository, prompts, policies), auth, "resources")}
}
func (a *GovernanceAPI) ServeHTTP(w http.ResponseWriter, r *http.Request) { a.handler.ServeHTTP(w, r) }
func NewPromptAPI(pool *pgxpool.Pool, auth *IdentityAPI, token string) *PromptAPI {
	_, p, _ := governancepostgres.NewRepositories(pool, "", nil, identitypostgres.RecordPrincipalAudit)
	return &PromptAPI{p}
}
func NewCreationPolicyAPI(pool *pgxpool.Pool, auth *IdentityAPI, token string) *CreationPolicyAPI {
	_, _, p := governancepostgres.NewRepositories(pool, "", nil, identitypostgres.RecordPrincipalAudit)
	return &CreationPolicyAPI{p}
}
func (a *GovernanceAPI) SetPromptCatalog(p *PromptAPI) {
	a.GovernanceRepository.SetPromptCatalog(p.PromptRepository)
}
func (a *GovernanceAPI) SetCreationPolicies(p *CreationPolicyAPI) {
	a.GovernanceRepository.SetCreationPolicies(p.PolicyRepository)
}

func identityTestPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	url := os.Getenv("MQ_TEST_CONTROL_DATABASE_URL")
	if url == "" {
		t.Skip("MQ_TEST_CONTROL_DATABASE_URL is not configured")
	}
	pool, err := pgxpool.New(context.Background(), url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return pool
}
func identityRequest(handler http.Handler, method, path, body string, cookie *http.Cookie) *httptest.ResponseRecorder {
	request := httptest.NewRequest(method, path, strings.NewReader(body))
	if cookie != nil {
		request.AddCookie(cookie)
		csrf := identityhttp.CSRF(cookie.Value)
		request.AddCookie(&http.Cookie{Name: "novel_agent_csrf", Value: csrf})
		request.Header.Set("X-CSRF-Token", csrf)
	}
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	return response
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
