// Package content owns immutable document versions and their application rules.
package content

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
)

var ErrInvalid = errors.New("invalid content IR")
var ErrConflict = errors.New("content version conflict")

type Version struct {
	ID       string          `json:"document_version_id"`
	Number   int             `json:"version"`
	Hash     string          `json:"content_hash"`
	Status   string          `json:"status"`
	Document json.RawMessage `json:"document"`
}

type VersionReference struct {
	ID   string
	Hash string
}

type GeneratedVersionCommit struct {
	WorkspaceID, ProjectID, JobID string
	DocumentID, VersionID         string
	DocumentType, SchemaVersion   string
	ContentHash                   string
	Payload                       json.RawMessage
	SourceVersionID, SourceIRHash string
	InputVersions                 []VersionReference
	WorkflowRunID, Workflow       string
}

type Repository interface {
	AppendVersion(context.Context, string, string, int, json.RawMessage, string) (Version, error)
}

type Service struct{ Repository Repository }

func (s Service) Append(ctx context.Context, scope, document string, expected int, body json.RawMessage) (Version, error) {
	var ir struct {
		SchemaVersion string            `json:"schema_version"`
		Blocks        []json.RawMessage `json:"blocks"`
	}
	if json.Unmarshal(body, &ir) != nil || ir.SchemaVersion != "1.0" || ir.Blocks == nil || expected < 0 {
		return Version{}, ErrInvalid
	}
	var value any
	if json.Unmarshal(body, &value) != nil {
		return Version{}, ErrInvalid
	}
	canonical, err := json.Marshal(value)
	if err != nil {
		return Version{}, err
	}
	sum := sha256.Sum256(canonical)
	return s.Repository.AppendVersion(ctx, scope, document, expected, canonical, hex.EncodeToString(sum[:]))
}
