package database

import (
	"testing"
	"time"
)

func TestParsePoolConfigAppliesConnectionLimits(t *testing.T) {
	settings, err := ParsePoolConfig(PoolConfig{
		URL:            "postgresql://control:secret@db.internal:5432/control?sslmode=require",
		MaxConnections: 80,
		MinConnections: 12,
		MaxIdleTime:    90 * time.Second,
	})
	if err != nil {
		t.Fatal(err)
	}
	if settings.ConnConfig.Host != "db.internal" {
		t.Fatalf("host = %q, want db.internal", settings.ConnConfig.Host)
	}
	if settings.MaxConns != 80 || settings.MinConns != 12 {
		t.Fatalf("pool limits = (%d, %d), want (80, 12)", settings.MaxConns, settings.MinConns)
	}
	if settings.MaxConnIdleTime != 90*time.Second {
		t.Fatalf("idle timeout = %s, want 1m30s", settings.MaxConnIdleTime)
	}
}

func TestParsePoolConfigRejectsMissingURL(t *testing.T) {
	if _, err := ParsePoolConfig(PoolConfig{}); err == nil {
		t.Fatal("expected missing URL to be rejected")
	}
}

func TestParsePoolConfigPreservesDisabledIdleTimeout(t *testing.T) {
	settings, err := ParsePoolConfig(PoolConfig{
		URL:            "postgresql://control:secret@db.internal:5432/control?sslmode=require",
		MaxConnections: 10,
		MinConnections: 0,
	})
	if err != nil {
		t.Fatal(err)
	}
	if settings.MaxConnIdleTime != 0 {
		t.Fatalf("idle timeout = %s, want disabled (0)", settings.MaxConnIdleTime)
	}
}

func TestParsePoolConfigRejectsInvalidConnectionLimits(t *testing.T) {
	_, err := ParsePoolConfig(PoolConfig{
		URL:            "postgresql://control:secret@db.internal:5432/control?sslmode=require",
		MaxConnections: 5,
		MinConnections: 6,
	})
	if err == nil {
		t.Fatal("expected minimum connections above maximum to be rejected")
	}
}
