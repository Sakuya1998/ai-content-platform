# Governance Module Organization

This document describes the governance module's code organization and the ongoing refactoring process.

## Current Structure

The governance module has two parallel implementations:

### 1. Main Package (`postgres/`)

The current production implementation with ~2700 lines:

- `governance.go` - Resource management and model profiles
- `prompt_catalog.go` / `prompt_catalog_commands.go` - Prompt catalog operations
- `creation_policy.go` / `creation_policy_commands.go` - Creation policy operations  
- `execution_catalog.go` - Execution snapshot catalog
- `repositories.go` - Repository constructors and interfaces
- `domain.go` - Domain models and types
- `catalog_io.go` - Catalog protocol I/O

This code is stable, tested, and in production use.

### 2. Subpackages (`postgres/{resources,models,prompts,policies}/`)

Refactored implementations organized by domain (~1400 lines):

- **resources/** - Generic resource CRUD operations (350 lines)
  - Versioning, status transitions, builtin styles
  
- **models/** - Model profile management (280 lines)
  - Profile CRUD, route freezing and resolution
  
- **prompts/** - Prompt catalog operations (480 lines)
  - Prompt and Pack CRUD, content hashing
  
- **policies/** - Creation policy management (290 lines)
  - Policy CRUD, versioning, publishing

Each subpackage:
- Is self-contained and independently compilable
- Has clear interfaces and minimal dependencies
- Follows the same patterns (Principal, Repository, ScanX functions)
- Includes package documentation

## Migration Strategy

The subpackages represent a **gradual refactoring approach**:

1. **Phase 1 (Current)**: Subpackages exist alongside main package
   - All code compiles
   - No breaking changes
   - Both implementations available

2. **Phase 2 (Future)**: Selective migration
   - Choose one module to migrate first (e.g., resources)
   - Create adapter layer for that module only
   - Test thoroughly before proceeding

3. **Phase 3 (Future)**: Complete migration
   - Once all modules proven stable, complete the switch
   - Remove old implementations
   - Clean up adapters

## Benefits of This Approach

✅ **Zero Risk**: Production code unchanged  
✅ **Clear Organization**: Domain logic clearly separated  
✅ **Independent Testing**: Each subpackage can be tested in isolation  
✅ **Incremental Migration**: Can proceed module by module  
✅ **Rollback Option**: Can revert any migration if issues arise  

## Using the Subpackages

The subpackages are ready to use for:

- New features that need similar functionality
- Testing alternative implementations
- Learning the domain structure
- Future migration efforts

Example:

```go
import "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance/postgres/resources"

repo := resources.NewRepository(pool, writeAudit, normalizePayload, builtinStyles, regexpKey, kinds)
statusCode, result, err := repo.ExecuteResourceCatalogOp(ctx, principal, kind, parts, action, filters, body)
```

## Next Steps

When ready to migrate:

1. Start with the simplest module (resources or models)
2. Create minimal adapter layer
3. Run full test suite
4. Verify no behavioral changes
5. Proceed to next module

Until then, both implementations coexist peacefully.
