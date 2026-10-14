# Novel Agent Platform Monorepo Makefile

.PHONY: help build test lint clean docker-up docker-down

help:
	@echo "Available commands:"
	@echo "  make build        - Build all services"
	@echo "  make test         - Run all tests"
	@echo "  make lint         - Lint all code"
	@echo "  make clean        - Clean build artifacts"
	@echo "  make docker-up    - Start Docker Compose platform"
	@echo "  make docker-down  - Stop Docker Compose platform"

# Build
build: build-control build-runtime

build-control:
	@echo "Building Control Plane..."
	cd apps/control && go build ./cmd/api

build-runtime:
	@echo "Building Runtime..."
	cd apps/runtime && uv sync --locked --all-extras

# Test
test: test-control test-runtime

test-control:
	@echo "Testing Control Plane..."
	cd apps/control && go test -race ./...

test-runtime:
	@echo "Testing Runtime..."
	cd apps/runtime && uv run --locked pytest

# Lint
lint: lint-control lint-runtime

lint-control:
	@echo "Linting Control Plane..."
	cd apps/control && go vet ./...

lint-runtime:
	@echo "Linting Runtime..."
	cd apps/runtime && uv run --locked ruff check .

# Clean
clean:
	@echo "Cleaning build artifacts..."
	cd apps/control && go clean
	rm -rf apps/runtime/.pytest_cache apps/runtime/.ruff_cache

# Docker
docker-up:
	@echo "Starting Docker Compose..."
	cd infra/docker && docker compose up -d

docker-down:
	@echo "Stopping Docker Compose..."
	cd infra/docker && docker compose down

# Development
dev-control:
	cd apps/control && go run ./cmd/api

dev-runtime:
	cd apps/runtime && uv run --locked uvicorn agent_runtime.api.app:app --reload

# Database migrations
migrate:
	cd apps/runtime && uv run --locked python ../../tools/migrate/platform_migrate.py

# Format code
format:
	cd apps/control && go fmt ./...
	cd apps/runtime && uv run --locked ruff format .
