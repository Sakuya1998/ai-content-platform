// Package prompts implements the prompt catalog functionality.
//
// This package handles:
//   - Prompt resource CRUD (individual prompts with messages and schemas)
//   - Prompt Pack CRUD (collections of prompts for workflows)
//   - Versioning and publishing workflow
//   - Prompt validation and normalization
//   - Workflow completeness checking
//
// Prompts are versioned resources with message arrays, variable schemas, and output schemas.
// Packs group prompts together with dependency resolution for complete workflow execution.
package prompts
