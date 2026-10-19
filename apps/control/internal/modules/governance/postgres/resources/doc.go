// Package resources implements generic governance resource management.
//
// This package handles:
//   - Generic resource CRUD (styles, content types, etc.)
//   - Resource versioning and status transitions
//   - Built-in resource handling (system styles)
//   - Execution snapshot creation
//
// Resources are versioned governance entities with typed payloads.
// They support draft/published/disabled states and can be cloned or copied.
package resources
