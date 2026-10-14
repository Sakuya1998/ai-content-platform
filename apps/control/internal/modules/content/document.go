package content

type GeneratedBlock struct {
	Type     string         `json:"type"`
	Text     string         `json:"text"`
	Position int            `json:"position"`
	Metadata map[string]any `json:"metadata"`
}
type GeneratedDocument struct {
	DocumentID    string           `json:"document_id"`
	ProjectID     string           `json:"project_id"`
	DocumentType  string           `json:"document_type"`
	Version       int              `json:"version"`
	SchemaVersion string           `json:"schema_version"`
	ContentHash   string           `json:"content_hash"`
	Blocks        []GeneratedBlock `json:"blocks"`
	Metadata      map[string]any   `json:"metadata"`
}
type ContentVersion struct {
	VersionID string            `json:"document_version_id"`
	Document  GeneratedDocument `json:"document"`
}
