// Package models implements model profile management and routing functionality.
//
// This package handles:
//   - Model profile CRUD operations (create, read, update, versions)
//   - Model route freezing and resolution
//   - Route target validation and fallback chains
//   - Model capability mapping for different generation types
//
// Model profiles define provider configurations (API keys, base URLs, available models).
// Routes map use cases (chat, embedding, video generation) to specific models within profiles.
package models
