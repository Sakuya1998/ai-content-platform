// Package database provides shared PostgreSQL connection and RLS transaction infrastructure.
package database

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type PoolConfig struct {
	URL            string
	MaxConnections int32
	MinConnections int32
	MaxIdleTime    time.Duration
}

func ParsePoolConfig(settings PoolConfig) (*pgxpool.Config, error) {
	if strings.TrimSpace(settings.URL) == "" {
		return nil, errors.New("PostgreSQL URL is required")
	}
	if settings.MaxConnections < 0 || settings.MinConnections < 0 || settings.MaxIdleTime < 0 {
		return nil, errors.New("PostgreSQL pool limits cannot be negative")
	}
	config, err := pgxpool.ParseConfig(settings.URL)
	if err != nil {
		return nil, fmt.Errorf("parse PostgreSQL pool config: %w", err)
	}
	if settings.MaxConnections > 0 {
		config.MaxConns = settings.MaxConnections
	}
	config.MinConns = settings.MinConnections
	config.MaxConnIdleTime = settings.MaxIdleTime
	if config.MinConns > config.MaxConns {
		return nil, errors.New("PostgreSQL minimum connections exceed maximum connections")
	}
	return config, nil
}

func Open(ctx context.Context, settings PoolConfig) (*pgxpool.Pool, error) {
	config, err := ParsePoolConfig(settings)
	if err != nil {
		return nil, err
	}
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		return nil, fmt.Errorf("initialize PostgreSQL pool: %w", err)
	}
	if err := pool.Ping(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("connect to PostgreSQL: %w", err)
	}
	return pool, nil
}

func BeginWorkspaceTx(ctx context.Context, pool *pgxpool.Pool, workspaceID string) (pgx.Tx, error) {
	if pool == nil {
		return nil, errors.New("PostgreSQL pool is required")
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, "SELECT set_config('app.workspace_id',$1,true)", workspaceID); err != nil {
		_ = tx.Rollback(ctx)
		return nil, err
	}
	return tx, nil
}
