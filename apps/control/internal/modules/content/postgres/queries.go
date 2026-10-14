package postgres

import (
	"bytes"
	"context"
	"encoding/json"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	contentmodule "github.com/sakuya/ai-content-platform/apps/control/internal/modules/content"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/project"
)

func CreateDocumentWithin(ctx context.Context, tx pgx.Tx, id, workspace, projectID, kind, title string) error {
	_, err := tx.Exec(ctx, "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES($1,$2,$3,$4,$5)", id, workspace, projectID, kind, title)
	return err
}

func ReadVersionWithin(ctx context.Context, tx pgx.Tx, workspace, projectID, versionID string) (contentmodule.ContentVersion, error) {
	result := contentmodule.ContentVersion{VersionID: versionID}
	var body []byte
	err := tx.QueryRow(ctx, `SELECT v.payload FROM document_versions v JOIN documents d ON d.id=v.document_id AND d.workspace_id=v.workspace_id
WHERE v.workspace_id=$1 AND d.project_id=$2 AND v.id=$3`, workspace, projectID, versionID).Scan(&body)
	if err != nil {
		return result, err
	}
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.UseNumber()
	err = decoder.Decode(&result.Document)
	return result, err
}

func NextVersionWithin(ctx context.Context, tx pgx.Tx, workspace, documentID string) (int, error) {
	var version int
	err := tx.QueryRow(ctx, "SELECT COALESCE(MAX(version),0)+1 FROM document_versions WHERE workspace_id=$1 AND document_id=$2", workspace, documentID).Scan(&version)
	return version, err
}

func VersionIDBySourceJob(ctx context.Context, tx pgx.Tx, workspace, jobID string) (string, error) {
	var versionID string
	err := tx.QueryRow(ctx, "SELECT id FROM document_versions WHERE workspace_id=$1 AND source_job_id=$2", workspace, jobID).Scan(&versionID)
	return versionID, err
}

func (r Repository) Documents(ctx context.Context, scope, projectID string) ([]map[string]any, error) {
	values := []map[string]any{}
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, "SELECT id,project_id,document_type,title,current_version_id FROM documents WHERE workspace_id=$1 AND ($2='' OR project_id::text=$2) ORDER BY updated_at DESC LIMIT 100", scope, projectID)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var id, project, kind, title string
			var version *string
			if err = rows.Scan(&id, &project, &kind, &title, &version); err != nil {
				return err
			}
			values = append(values, map[string]any{"document_id": id, "project_id": project, "document_type": kind, "title": title, "current_version_id": version})
		}
		return rows.Err()
	})
	return values, err
}
func (r Repository) Document(ctx context.Context, scope, document string) (map[string]any, error) {
	var id, project, kind, title string
	var version *string
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT id,project_id,document_type,title,current_version_id FROM documents WHERE workspace_id=$1 AND id=$2", scope, document).Scan(&id, &project, &kind, &title, &version)
	})
	return map[string]any{"document_id": id, "project_id": project, "document_type": kind, "title": title, "current_version_id": version}, err
}
func (r Repository) VersionByID(ctx context.Context, scope, id, projectID string) (json.RawMessage, error) {
	var body []byte
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT v.payload FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE v.workspace_id=$1 AND v.id=$2 AND ($3='' OR d.project_id::text=$3)", scope, id, projectID).Scan(&body)
	})
	return body, err
}
func (r Repository) Version(ctx context.Context, scope, document string, number int) (json.RawMessage, error) {
	var body []byte
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT payload FROM document_versions WHERE workspace_id=$1 AND document_id=$2 AND version=$3", scope, document, number).Scan(&body)
	})
	return body, err
}
func (r Repository) Versions(ctx context.Context, scope, document string) ([]map[string]any, error) {
	values := []map[string]any{}
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		var exists bool
		if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM documents WHERE workspace_id=$1 AND id=$2)", scope, document).Scan(&exists); err != nil {
			return err
		}
		if !exists {
			return project.ErrNotFound
		}
		rows, err := tx.Query(ctx, "SELECT id,version,content_hash,status,payload FROM document_versions WHERE workspace_id=$1 AND document_id=$2 ORDER BY version DESC LIMIT 100", scope, document)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var id, hash, status string
			var number int
			var body []byte
			if err = rows.Scan(&id, &number, &hash, &status, &body); err != nil {
				return err
			}
			values = append(values, map[string]any{"document_version_id": id, "version": number, "content_hash": hash, "status": status, "document": json.RawMessage(body)})
		}
		return rows.Err()
	})
	return values, err
}
func (r Repository) CreateDocument(ctx context.Context, scope, projectID, kind, title string) (map[string]any, error) {
	id := uuid.NewString()
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		var exists bool
		if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM projects WHERE workspace_id=$1 AND id=$2)", scope, projectID).Scan(&exists); err != nil {
			return err
		}
		if !exists {
			return project.ErrNotFound
		}
		_, err := tx.Exec(ctx, "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES($1,$2,$3,$4,$5)", id, scope, projectID, kind, title)
		return err
	})
	return map[string]any{"document_id": id, "project_id": projectID, "document_type": kind, "title": title}, err
}
