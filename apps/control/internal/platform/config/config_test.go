package config

import "testing"

func TestHierarchicalMQChannels(t *testing.T) {
	t.Setenv("CONTROL_PLANE_SERVER_ENVIRONMENT", "prod")
	t.Setenv("CONTROL_PLANE_MQ_TOPICS_CONTROL", "custom-control")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.MQ.Topics.Tasks != "agent-prod-runtime-tasks" || cfg.MQ.Topics.Control != "custom-control" || cfg.MQ.Groups.Projection != "agent-prod-control-projection" {
		t.Fatal("MQ channels are not environment scoped or overridden")
	}
}

func TestLoadReadsControlPlaneEnvironment(t *testing.T) {
	t.Setenv("CONTROL_PLANE_SERVER_HTTP_ADDR", ":9090")
	t.Setenv("CONTROL_PLANE_DATABASE_ADDR", "postgres-control:5432")
	t.Setenv("CONTROL_PLANE_DATABASE_DB", "control")
	t.Setenv("CONTROL_PLANE_DATABASE_USERNAME", "control")
	t.Setenv("CONTROL_PLANE_DATABASE_PASSWORD", "secret")
	t.Setenv("CONTROL_PLANE_SECURITY_ALLOW_MEMORY", "true")

	loaded, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Server.HTTPAddr != ":9090" || loaded.Database.ConnectionURL() != "postgresql://control:secret@postgres-control:5432/control" || !loaded.Security.AllowMemory {
		t.Fatalf("unexpected config: %+v", loaded)
	}
}

func TestLoadReadsExecutionEncryptionKey(t *testing.T) {
	t.Setenv("CONTROL_PLANE_SECURITY_EXECUTION_ENCRYPTION_KEY", "execution-key-test")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Security.ExecutionEncryptionKey != "execution-key-test" {
		t.Fatal("execution encryption key was not loaded")
	}
}

func TestProxyAndSharedInfrastructureConfig(t *testing.T) {
	t.Setenv("CONTROL_PLANE_MQ_ENDPOINT", "proxy.example.com:8081")
	t.Setenv("CONTROL_PLANE_MQ_TLS_ENABLED", "true")
	t.Setenv("CONTROL_PLANE_MQ_ACCESS_KEY", "test-ak")
	t.Setenv("CONTROL_PLANE_MQ_SECRET_KEY", "test-sk")
	t.Setenv("CONTROL_PLANE_OBJECT_STORAGE_PROVIDERS_0_BUCKET", "agent-artifacts")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.MQ.Endpoint != "proxy.example.com:8081" || !cfg.MQ.TLSEnabled || cfg.MQ.AccessKey != "test-ak" {
		t.Fatal("Proxy configuration was not loaded")
	}
	if cfg.Redis.DB != 0 || cfg.Redis.KeyPrefix != "control:" || cfg.ObjectStorage.Providers[0].Bucket != "agent-artifacts" {
		t.Fatal("shared infrastructure configuration was not loaded")
	}
}

func TestIndexedObjectStorageEndpointPreservesProviderDefaults(t *testing.T) {
	t.Setenv("CONTROL_PLANE_OBJECT_STORAGE_PROVIDERS_0_ENDPOINT", "http://rustfs:9000")
	t.Setenv("CONTROL_PLANE_OBJECT_STORAGE_PROVIDERS_0_ACCESS_KEY", "fake-key")
	t.Setenv("CONTROL_PLANE_OBJECT_STORAGE_PROVIDERS_0_SECRET_KEY", "fake-secret")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	provider := cfg.ObjectStorage.Providers[0]
	if provider.Type != cfg.ObjectStorage.Default || provider.Bucket != "agent-artifacts" || provider.Region != "us-east-1" {
		t.Fatal("indexed environment provider lost default type, bucket or region")
	}
}
