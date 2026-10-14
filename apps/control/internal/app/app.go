package app

import (
	"context"
	"errors"
	"log/slog"
	"time"

	contentpostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/content/postgres"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/evaluation"
	evaluationpostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/evaluation/postgres"
	executionpostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/execution/postgres"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/governance"
	governancepostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/governance/postgres"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/identity"
	identitypostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/identity/postgres"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/knowledge"
	knowledgepostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/knowledge/postgres"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/observability"
	observabilitypostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/observability/postgres"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/production"
	productionpostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/production/postgres"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/workflow"
	workflowpostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/workflow/postgres"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/config"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/database"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/messaging"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/modelprovider"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/objectstorage"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/password"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/security"
	httptransport "github.com/sakuya/novel-agent/go-control-plane/internal/transport/http"
	"github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/api"
	governancehttp "github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/governance"
	identityhttp "github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/identity"
	"github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/operations"
	productionhttp "github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/production"
	"github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/resources"
	workflowhttp "github.com/sakuya/novel-agent/go-control-plane/internal/transport/http/workflow"
)

func New(ctx context.Context, cfg config.Config, logger *slog.Logger) (*Application, error) {
	if logger == nil {
		return nil, errors.New("structured logger is required")
	}
	if cfg.Database.ConnectionURL() == "" && !cfg.Security.AllowMemory {
		return nil, errors.New("persistent Control database is required")
	}
	if err := cfg.Validate(); err != nil {
		return nil, errors.New("invalid control-plane settings")
	}
	a := &Application{logger: logger, address: cfg.Server.HTTPAddr}
	initialized := false
	defer func() {
		if !initialized {
			a.Close()
		}
	}()
	dependencies := api.ServerDependencies{}
	readinessChecks := []func(context.Context) error{}
	var artifacts *objectstorage.S3Store
	for _, provider := range cfg.ObjectStorage.Providers {
		if provider.Type != cfg.ObjectStorage.Default || provider.Endpoint == "" {
			continue
		}
		var err error
		artifacts, err = objectstorage.NewS3Store(provider)
		if err != nil {
			return nil, errors.New("invalid artifact storage configuration")
		}
		dependencies.Artifacts = artifacts
		readinessChecks = append(readinessChecks, artifacts.Ready)
		break
	}
	if cfg.MQ.Enabled && artifacts == nil {
		return nil, errors.New("MQ execution requires a configured artifact store")
	}
	var messages *executionpostgres.MQRepository
	if databaseURL := cfg.Database.ConnectionURL(); databaseURL != "" {
		startup, stop := context.WithTimeout(ctx, 10*time.Second)
		pool, err := database.Open(startup, database.PoolConfig{
			URL: databaseURL, MaxConnections: int32(cfg.Database.MaxConnections),
			MinConnections: int32(cfg.Database.MinConnections),
			MaxIdleTime:    time.Duration(cfg.Database.IdleTimeoutSecs) * time.Second,
		})
		stop()
		if err != nil {
			return nil, errors.New("Control database initialization failed")
		}
		a.cleanup = append(a.cleanup, pool.Close)
		readinessChecks = append(readinessChecks, pool.Ping)
		identityService := identity.NewService(identitypostgres.NewRepository(pool), password.Codec{}, cfg.Security.SessionHours)
		identity := identityhttp.NewHandler(identityService, identityhttp.Settings{
			Enabled: cfg.Security.AuthEnabled, CookieSecure: cfg.Security.CookieSecure,
			SessionHours: cfg.Security.SessionHours, AllowedOrigins: cfg.Security.AllowedOrigins,
		})
		governanceRepo, prompts, policies := governancepostgres.NewRepositories(pool, cfg.Security.ModelEncryptionKey, modelprovider.NewModelTester(), identitypostgres.RecordPrincipalAudit)
		cipher, err := security.NewExecutionCipher(cfg.Security.ExecutionEncryptionKey)
		if err != nil {
			return nil, errors.New("invalid execution encryption key")
		}
		catalog := governance.NewCatalogService(governanceRepo, prompts, policies)
		workflowService := workflow.NewService(workflowpostgres.NewRepository(pool, identitypostgres.RecordPrincipalAudit))
		freezeSnapshot := func(ctx context.Context, workspace string, base map[string]any) (map[string]any, error) {
			template, _ := base["workflow"].(string)
			version, err := workflowService.Frozen(ctx, workspace, template)
			if err != nil {
				return nil, err
			}
			frozen, err := governanceRepo.FreezeCapabilitySnapshot(ctx, workspace, base)
			if err != nil {
				return nil, err
			}
			frozen["workflow_definition"] = map[string]any{
				"version_id": version.ID, "name": version.Name, "version": version.Version,
				"content_hash": version.ContentHash, "definition": version.Definition,
			}
			return frozen, nil
		}
		executionResources := governance.NewExecutionService(governanceRepo, cipher)
		var reader resources.ArtifactReader
		if artifacts != nil {
			reader = artifacts
		}
		settings := executionpostgres.RepositorySettings{
			ContentArtifacts: reader, ResourceResolver: executionResources.Resolve,
			SnapshotFreezer:        freezeSnapshot,
			Projector:              executionpostgres.NewEventProjector(reader, 0, contentpostgres.PersistContentRecord),
			FrozenModelProfile:     governancepostgres.FrozenModelProfile,
			BenchmarkWriter:        evaluationpostgres.RecordBenchmark,
			BenchmarkReader:        evaluationpostgres.ReadBenchmarkWithin,
			BenchmarkReportResolver: evaluationpostgres.ResolveBenchmarkReport,
			PrepareContentDocument: contentpostgres.PrepareGeneratedDocument,
			CommitContentVersion:   contentpostgres.CommitGeneratedVersion,
			ReadContentVersion:     contentpostgres.ReadVersionWithin,
			VersionIDBySourceJob:   contentpostgres.VersionIDBySourceJob,
		}
		if artifacts != nil {
			settings.SnapshotStore = artifacts
		}
		messages = executionpostgres.NewMQRepository(pool, cfg.MQ.Topics, settings)
		resourceHandler := newResourceHandler(pool, messages, reader, resourceOptions{
			SnapshotFreezer:       freezeSnapshot,
			InputFreezer:          governanceRepo.FreezeContentInput,
			TransferEncryptionKey: cfg.Security.ModelEncryptionKey,
		})
		dependencies.Identity, dependencies.Governance = identity, governancehttp.NewHandler(catalog, identity, "resources")
		dependencies.Workflows = workflowhttp.NewHandler(workflowService)
		dependencies.Prompts, dependencies.CreationPolicies = governancehttp.NewHandler(catalog, identity, "prompts"), governancehttp.NewHandler(catalog, identity, "policies")
		dependencies.Resources = resourceHandler
		productionRepository := productionpostgres.NewRepository(pool, messages, productionpostgres.RepositorySettings{
			SnapshotFreezer: freezeSnapshot, FrozenModelProfile: governancepostgres.FrozenModelProfile,
			ContentVersion: contentpostgres.ReadVersionWithin, VersionIDBySourceJob: contentpostgres.VersionIDBySourceJob,
		})
		dependencies.Production = productionhttp.NewHandler(production.Service{Repository: productionRepository})
		dependencies.Operations = operations.NewOperationsAPI(
			knowledge.NewService(knowledgepostgres.NewRepository(pool)),
			evaluation.NewService(evaluationpostgres.NewRepository(pool)),
			observability.NewService(observabilitypostgres.NewRepository(pool)),
			messages,
			cfg.Security.InternalToken,
		)
		a.background = append(a.background,
			periodic(logger, "transfer recovery", time.Minute, resourceHandler.RecoverTransfers),
			periodic(logger, "production recovery", 2*time.Second, productionRepository.RecoverProductions))
	}
	if cfg.MQ.Enabled {
		if messages == nil {
			return nil, errors.New("MQ requires a persistent Control database")
		}
		client, err := messaging.NewRocketMQClient(cfg.MQ)
		if err != nil {
			return nil, errors.New("invalid MQ configuration")
		}
		a.cleanup = append(a.cleanup, func() { _ = client.Close() })
		if err = client.Start(); err != nil {
			return nil, errors.New("MQ Proxy SDK startup failed")
		}
		a.background = append(a.background, backgroundTask{name: "messaging", run: func(ctx context.Context) {
			messaging.RunMessaging(ctx, messages, client, logger)
		}})
	}
	dependencies.Ready = func(ctx context.Context) error {
		for _, check := range readinessChecks {
			if err := check(ctx); err != nil {
				return err
			}
		}
		return nil
	}
	startup, stop := context.WithTimeout(ctx, 10*time.Second)
	err := dependencies.Ready(startup)
	stop()
	if err != nil {
		return nil, errors.New("Control dependencies are unavailable")
	}
	a.handler = httptransport.NewRouter(api.NewServerWithDependencies(dependencies), logger, cfg.Security.AllowedOrigins)
	initialized = true
	return a, nil
}
