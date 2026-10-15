package governance

import "errors"

// Common governance errors
var (
	ErrNotFound          = errors.New("resource not found")
	ErrConflict          = errors.New("resource conflict")
	ErrMethodNotAllowed  = errors.New("method not allowed")
	ErrInvalidKey        = errors.New("invalid resource key")
	ErrNameRequired      = errors.New("name is required")
	ErrKeyImmutable      = errors.New("key is immutable")
	ErrParentNotFound    = errors.New("parent resource not found")
	ErrInvalidTarget     = errors.New("invalid route target")
	ErrInvalidKind       = errors.New("invalid model kind")
	ErrModelNotFound     = errors.New("model not found in profile")
)
