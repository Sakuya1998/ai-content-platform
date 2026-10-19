# Governance Module Migration Status

## Current State

The PostgreSQL adapter has been migrated into domain-focused subpackages. `resources` owns generic resource CRUD and frozen resource reads; `models` owns model profile CRUD, credential encryption, route freezing, and frozen route resolution; `prompts` owns Prompt, Pack, workflow binding, and frozen prompt validation; `policies` owns creation policy and workflow binding operations. `GovernanceRepository` remains the compatibility facade used by the catalog service and execution code.

The old resource CRUD and model catalog/route implementations have been removed from `governance.go`. The facade delegates resource and model operations to their subpackages while preserving result kinds, HTTP status mapping, built-in style handling, payload normalization, audit writes, credential masking, and workspace scoping through `database.BeginWorkspaceTx`.

## Verification

Resource unit and PostgreSQL lifecycle tests cover create, publish, update, frozen-version immutability, and cross-workspace isolation. Prompt and Policy integration tests cover versioning, publishing, rollback, bindings, frozen validation, and catalog initialization. Model tests cover API key masking, route version freezing, fallback resolution, and media capabilities. `go test -race ./...` and `go vet ./...` passed against PostgreSQL 17 with a non-superuser application role and initialized Prompt/Policy catalogs.

## Architecture

The facade preserves service-facing interfaces and catalog result mapping. Each repository uses the shared `database.BeginWorkspaceTx` helper for tenant RLS context, and each subpackage owns its SQL and domain-specific persistence behavior. Generic execution snapshots remain in the parent package because they combine model route metadata with resource versions.
