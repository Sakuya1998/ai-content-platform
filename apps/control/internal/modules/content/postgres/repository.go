package postgres

import (
	"context"
	"encoding/json"
	"fmt"

	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	contentmodule "github.com/sakuya/novel-agent/apps/control/internal/modules/content"
	taskmodule "github.com/sakuya/novel-agent/apps/control/internal/modules/task"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/database"
)

type Repository struct {
	Pool       *pgxpool.Pool
	EnsureIdle func(context.Context, pgx.Tx, string, string) error
	Conflict   error
	Invalid    error
}

func PrepareGeneratedDocument(ctx context.Context, tx pgx.Tx, workspace, projectID, documentID, documentType, title string) (int, error) {
	if _, err := tx.Exec(ctx, "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES($1,$2,$3,$4,$5) ON CONFLICT(id) DO NOTHING", documentID, workspace, projectID, documentType, title); err != nil {
		return 0, err
	}
	var actualProject, actualType string
	if err := tx.QueryRow(ctx, "SELECT project_id,document_type FROM documents WHERE workspace_id=$1 AND id=$2 FOR UPDATE", workspace, documentID).Scan(&actualProject, &actualType); err != nil {
		return 0, err
	}
	if actualProject != projectID || actualType != documentType {
		return 0, contentmodule.ErrConflict
	}
	var version int
	if err := tx.QueryRow(ctx, "SELECT COALESCE(MAX(version),0)+1 FROM document_versions WHERE workspace_id=$1 AND document_id=$2", workspace, documentID).Scan(&version); err != nil {
		return 0, err
	}
	return version, nil
}

func CommitGeneratedVersion(ctx context.Context, tx pgx.Tx, commit contentmodule.GeneratedVersionCommit) error {
	var document contentmodule.GeneratedDocument
	if err := json.Unmarshal(commit.Payload, &document); err != nil {
		return err
	}
	var existing string
	err := tx.QueryRow(ctx, "SELECT id FROM document_versions WHERE workspace_id=$1 AND source_job_id=$2", commit.WorkspaceID, commit.JobID).Scan(&existing)
	if err == nil {
		return nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return err
	}
	var actualProject, actualType string
	if err = tx.QueryRow(ctx, "SELECT project_id,document_type FROM documents WHERE workspace_id=$1 AND id=$2 FOR UPDATE", commit.WorkspaceID, commit.DocumentID).Scan(&actualProject, &actualType); err != nil {
		return err
	}
	if actualProject != commit.ProjectID || actualType != commit.DocumentType {
		return contentmodule.ErrConflict
	}
	var previous int
	if err = tx.QueryRow(ctx, "SELECT COALESCE(MAX(version),0) FROM document_versions WHERE workspace_id=$1 AND document_id=$2", commit.WorkspaceID, commit.DocumentID).Scan(&previous); err != nil {
		return err
	}
	if previous+1 != document.Version {
		return contentmodule.ErrConflict
	}
	if document.ProjectID != commit.ProjectID || document.DocumentID != commit.DocumentID || document.DocumentType != commit.DocumentType || document.Version != previous+1 || document.SchemaVersion != commit.SchemaVersion || document.ContentHash != commit.ContentHash {
		return contentmodule.ErrConflict
	}
	for index, block := range document.Blocks {
		if block.Text == "" || block.Type == "" || block.Position != index {
			return contentmodule.ErrInvalid
		}
	}
	if _, err = tx.Exec(ctx, `INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,content_hash,payload,status,source_job_id)
        VALUES($1,$2,$3,$4,$5,$6,$7,'approved',$8)`, commit.VersionID, commit.WorkspaceID, commit.DocumentID, document.Version, commit.SchemaVersion, commit.ContentHash, commit.Payload, commit.JobID); err != nil {
		return err
	}
	if err = PersistContentBlocks(ctx, tx, commit.WorkspaceID, commit.VersionID, commit.Payload); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, "UPDATE documents SET current_version_id=$1,updated_at=now() WHERE workspace_id=$2 AND id=$3", commit.VersionID, commit.WorkspaceID, commit.DocumentID); err != nil {
		return err
	}
	if commit.SourceVersionID != "" {
		if _, err = tx.Exec(ctx, "INSERT INTO derivations(workspace_id,source_version_id,target_version_id,workflow_run_id,workflow_template_version,source_ir_hash) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING", commit.WorkspaceID, commit.SourceVersionID, commit.VersionID, commit.WorkflowRunID, commit.Workflow, commit.SourceIRHash); err != nil {
			return err
		}
	}
	for _, input := range commit.InputVersions {
		if input.ID == commit.SourceVersionID {
			continue
		}
		if _, err = tx.Exec(ctx, "INSERT INTO derivations(workspace_id,source_version_id,target_version_id,workflow_run_id,workflow_template_version,source_ir_hash) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING", commit.WorkspaceID, input.ID, commit.VersionID, commit.WorkflowRunID, commit.Workflow, input.Hash); err != nil {
			return err
		}
	}
	return nil
}

func (r Repository) Within(ctx context.Context, scope string, fn func(pgx.Tx) error) error {
	return r.within(ctx, scope, fn)
}

func (r Repository) AppendVersion(ctx context.Context, scope, document string, expected int, body json.RawMessage, hash string) (contentmodule.Version, error) {
	var version contentmodule.Version
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		var project string
		if err := tx.QueryRow(ctx, "SELECT project_id FROM documents WHERE workspace_id=$1 AND id=$2 FOR UPDATE", scope, document).Scan(&project); err != nil {
			return err
		}
		if err := r.EnsureIdle(ctx, tx, scope, project); err != nil {
			return err
		}
		var current int
		if err := tx.QueryRow(ctx, "SELECT COALESCE(MAX(version),0) FROM document_versions WHERE workspace_id=$1 AND document_id=$2", scope, document).Scan(&current); err != nil {
			return err
		}
		if expected != current {
			return r.Conflict
		}
		version = contentmodule.Version{ID: uuid.NewString(), Number: current + 1, Hash: hash, Status: "draft", Document: body}
		if _, err := tx.Exec(ctx, "INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,content_hash,payload,status) VALUES($1,$2,$3,$4,'1.0',$5,$6,'draft')", version.ID, scope, document, version.Number, hash, body); err != nil {
			return err
		}
		if err := PersistContentBlocks(ctx, tx, scope, version.ID, body); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, "UPDATE documents SET current_version_id=$1,updated_at=now() WHERE workspace_id=$2 AND id=$3", version.ID, scope, document)
		return err
	})
	return version, err
}

func PersistContentBlocks(ctx context.Context, tx pgx.Tx, scope, version string, body []byte) error {
	var ir struct {
		Blocks []map[string]any `json:"blocks"`
	}
	if err := json.Unmarshal(body, &ir); err != nil {
		return err
	}
	for index, block := range ir.Blocks {
		kind := text(block["type"])
		if kind == "" {
			kind = "text"
		}
		if _, err := tx.Exec(ctx, "INSERT INTO content_blocks(workspace_id,document_version_id,block_index,block_type,payload) VALUES($1,$2,$3,$4,$5)", scope, version, index, kind, jsonBytes(block)); err != nil {
			return err
		}
	}
	return nil
}

func PersistContentRecord(ctx context.Context, tx pgx.Tx, scope, project, job string, row map[string]any, invalid, conflict error) error {
	index := integer(row["block_index"])
	if index < 1 {
		return invalid
	}
	version := integer(row["version_number"])
	if version < 1 {
		version = integer(row["version"])
	}
	if version < 1 {
		return invalid
	}
	var document string
	title := fmt.Sprintf("Block %d", index)
	if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", scope+":"+project+":"+title); err != nil {
		return err
	}
	err := tx.QueryRow(ctx, "SELECT id FROM documents WHERE workspace_id=$1 AND project_id=$2 AND document_type='block' AND title=$3 ORDER BY created_at,id LIMIT 1 FOR UPDATE", scope, project, title).Scan(&document)
	if errors.Is(err, pgx.ErrNoRows) {
		document = uuid.NewSHA1(uuid.NameSpaceURL, []byte("content-block:"+scope+":"+project+":"+title)).String()
		_, err = tx.Exec(ctx, "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES($1,$2,$3,'block',$4)", document, scope, project, title)
	}
	if err != nil {
		return err
	}
	payload := map[string]any{}
	for key, value := range row {
		if key != "record_id" && key != "record_key" && key != "stale" && key != "baseline" {
			payload[key] = value
		}
	}
	recordBody := jsonBytes(payload)
	key := text(row["id"])
	if key == "" {
		key = fmt.Sprintf(":%d:%d", index, version)
	}
	id := uuid.NewSHA1(uuid.NameSpaceURL, []byte(scope+":"+project+":content:"+key)).String()
	body := jsonBytes(map[string]any{"schema_version": "1.0", "blocks": []map[string]any{{"type": "text", "text": text(row["content"]), "position": 0}}, "record": payload, "record_key": key, "record_hash": hash(recordBody), "block_index": index, "source_job_id": job})
	tag, err := tx.Exec(ctx, "INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,content_hash,payload,status) VALUES($1,$2,$3,$4,'1.0',$5,$6,'draft') ON CONFLICT(document_id,version) DO NOTHING", id, scope, document, version, hash(body), body)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		var prior []byte
		if err = tx.QueryRow(ctx, "SELECT payload->'record' FROM document_versions WHERE workspace_id=$1 AND document_id=$2 AND version=$3", scope, document, version).Scan(&prior); err != nil {
			return err
		}
		if !equalJSON(prior, recordBody) {
			return conflict
		}
		return nil
	}
	if err = PersistContentBlocks(ctx, tx, scope, id, body); err != nil {
		return err
	}
	_, err = tx.Exec(ctx, "UPDATE documents SET current_version_id=(SELECT id FROM document_versions WHERE document_id=$1 ORDER BY version DESC LIMIT 1),updated_at=now() WHERE id=$1", document)
	return err
}

func (r Repository) within(ctx context.Context, scope string, fn func(pgx.Tx) error) error {
	if r.Pool == nil {
		return errors.New("content database unavailable")
	}
	tx, err := database.BeginWorkspaceTx(ctx, r.Pool, scope)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}
func integer(value any) int      { return taskmodule.Integer(value) }
func jsonBytes(value any) []byte { body, _ := json.Marshal(value); return body }
func text(value any) string      { valueText, _ := value.(string); return valueText }
func hash(body []byte) string    { sum := sha256.Sum256(body); return hex.EncodeToString(sum[:]) }
func equalJSON(a, b []byte) bool {
	var left, right any
	first, second := json.NewDecoder(bytes.NewReader(a)), json.NewDecoder(bytes.NewReader(b))
	first.UseNumber()
	second.UseNumber()
	if first.Decode(&left) != nil || second.Decode(&right) != nil {
		return false
	}
	return bytes.Equal(jsonBytes(left), jsonBytes(right))
}
