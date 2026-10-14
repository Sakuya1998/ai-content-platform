package config

import "testing"

func TestLoadRejectsInvalidConnectionLimits(t *testing.T) {
	t.Setenv("CONTROL_PLANE_DATABASE_MAX_CONNECTIONS", "2")
	t.Setenv("CONTROL_PLANE_DATABASE_MIN_CONNECTIONS", "3")
	if _, err := Load(); err == nil {
		t.Fatal("invalid pool limits were accepted")
	}
}

func TestLoadUsesSingleControlDatabaseURLOverride(t *testing.T) {
	t.Setenv("CONTROL_DATABASE_URL", "postgresql://control@host/selected")
	t.Setenv("CONTROL_PLANE_DATABASE_URL", "postgresql://control@host/new")
	cfg, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Database.ConnectionURL() != "postgresql://control@host/selected" {
		t.Fatal("Control database URL override was not loaded")
	}
}

func TestLoadRejectsProductionWithoutAuthentication(t *testing.T) {
	t.Setenv("CONTROL_PLANE_SERVER_ENVIRONMENT", "production")
	t.Setenv("CONTROL_PLANE_SECURITY_AUTH_ENABLED", "false")
	if _, err := Load(); err == nil {
		t.Fatal("production without authentication was accepted")
	}
}

func TestLoadRejectsProductionMemoryMode(t *testing.T) {
	t.Setenv("CONTROL_PLANE_SERVER_ENVIRONMENT", "production")
	t.Setenv("CONTROL_PLANE_SECURITY_ALLOW_MEMORY", "true")
	if _, err := Load(); err == nil {
		t.Fatal("production memory mode bypasses persistent identity")
	}
}

func TestProductionMQRequiresEncryptionKeys(t *testing.T) {
	t.Setenv("CONTROL_PLANE_SERVER_ENVIRONMENT", "production")
	t.Setenv("CONTROL_PLANE_MQ_ENABLED", "true")
	t.Setenv("CONTROL_PLANE_MQ_ENDPOINT", "proxy:8081")
	if _, err := Load(); err == nil {
		t.Fatal("production MQ accepted missing encryption keys")
	}
}
