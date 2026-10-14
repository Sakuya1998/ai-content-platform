package postgres

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/execution"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project/transfer"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/task"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/database"
	"time"
)

type ContentRecordWriter = func(context.Context, pgx.Tx, string, string, string, map[string]any, error, error) error
type LoadProjectFunc = func(context.Context, pgx.Tx, string, string, bool) (project.Resource, error)
type CreateProjectWithinFunc = func(context.Context, pgx.Tx, string, map[string]any, string) (project.Resource, error)
type AuditFunc = func(context.Context, pgx.Tx, string, string, string) error
type ReadRecordsFunc = func(context.Context, pgx.Tx, string, string, string, int, string) ([]map[string]any, error)
type EnvelopeWriterFunc = func(context.Context, pgx.Tx, string, string, string, string, execution.Envelope, error, error, ContentRecordWriter) error

type Repository struct {
	Pool                *pgxpool.Pool
	Conflict            error
	Invalid             error
	LoadProject         LoadProjectFunc
	CreateProjectWithin CreateProjectWithinFunc
	Audit               AuditFunc
	ReadRecords         ReadRecordsFunc
	PersistEnvelope     EnvelopeWriterFunc
	WriteContentRecord  ContentRecordWriter
}

func jsonBytes(value any) []byte { body, _ := json.Marshal(value); return body }
func text(value any) string      { valueText, _ := value.(string); return valueText }
func hash(body []byte) string    { sum := sha256.Sum256(body); return hex.EncodeToString(sum[:]) }

func (r Repository) within(ctx context.Context, scope string, fn func(pgx.Tx) error) error {
	if r.Pool == nil {
		return errors.New("project transfer database unavailable")
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
func (r Repository) Import(ctx context.Context, scope, inputKey string, backup transfer.Backup) (project.Resource, error) {
	if r.LoadProject == nil || r.CreateProjectWithin == nil || r.PersistEnvelope == nil || r.Audit == nil {
		return project.Resource{}, errors.New("project transfer dependencies unavailable")
	}
	key := ""
	var novel project.Resource
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", scope+":import:"+inputKey); err != nil {
			return err
		}
		var previous string
		err := tx.QueryRow(ctx, "SELECT project_id,fingerprint FROM project_request_keys WHERE workspace_id=$1 AND key=$2", scope, inputKey).Scan(&previous, &key)
		if err == nil {
			if key != backup.SHA256 {
				return r.Conflict
			}
			novel, err = r.LoadProject(ctx, tx, scope, previous, false)
			return err
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		novel, err = r.CreateProjectWithin(ctx, tx, scope, backup.ProjectResource, "")
		if err != nil {
			return err
		}
		if backup.Envelope.State == nil {
			backup.Envelope.State = map[string]any{}
		}
		backup.Envelope.State["project_id"] = novel.ID
		remapEnvelope(&backup.Envelope, novel.ID)
		if err = r.PersistEnvelope(ctx, tx, scope, novel.ID, "", "", backup.Envelope, r.Invalid, r.Conflict, r.WriteContentRecord); err != nil {
			return err
		}
		for _, brief := range backup.BriefVersions {
			version := task.Integer(brief["version"])
			if version < 1 {
				continue
			}
			payload := brief["brief"]
			if payload == nil {
				payload = brief["payload"]
			}
			if payload == nil {
				return r.Invalid
			}
			if version == 1 {
				if _, err = tx.Exec(ctx, "UPDATE project_brief_versions SET payload=$1,change_summary=$2 WHERE workspace_id=$3 AND project_id=$4 AND version=1", jsonBytes(payload), text(brief["change_summary"]), scope, novel.ID); err != nil {
					return err
				}
				continue
			}
			if _, err = tx.Exec(ctx, "INSERT INTO project_brief_versions(workspace_id,project_id,version,payload,change_summary) VALUES($1,$2,$3,$4,$5)", scope, novel.ID, version, jsonBytes(payload), text(brief["change_summary"])); err != nil {
				return err
			}
			if _, err = tx.Exec(ctx, "UPDATE project_projections SET brief_version=GREATEST(brief_version,$1) WHERE workspace_id=$2 AND id=$3", version, scope, novel.ID); err != nil {
				return err
			}
		}
		if _, err = tx.Exec(ctx, "INSERT INTO project_request_keys(workspace_id,key,fingerprint,project_id) VALUES($1,$2,$3,$4)", scope, inputKey, backup.SHA256, novel.ID); err != nil {
			return err
		}
		if err = r.Audit(ctx, tx, scope, "project.imported", novel.ID); err != nil {
			return err
		}
		novel, err = r.LoadProject(ctx, tx, scope, novel.ID, false)
		return err
	})

	return novel, err
}
func (r Repository) Backup(ctx context.Context, scope, id string) (transfer.Backup, error) {
	if r.LoadProject == nil || r.ReadRecords == nil {
		return transfer.Backup{}, errors.New("project transfer snapshot reader unavailable")
	}
	backup := transfer.Backup{SchemaVersion: "1.0"}
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		// Every projection and brief edit locks this row, giving the export a coherent snapshot.
		n, err := r.LoadProject(ctx, tx, scope, id, true)
		if err != nil {
			return err
		}
		backup.ProjectResource = n.Input
		backup.Envelope.State = n.State
		targets := map[string]*[]map[string]any{"content": &backup.Envelope.ContentVersions, "planning": &backup.Envelope.PlanningVersions, "candidate": &backup.Envelope.Candidates, "evaluation": &backup.Envelope.Evaluations, "audit": &backup.Envelope.Audits, "memory": &backup.Envelope.MemorySnapshots, "memory_quality": &backup.Envelope.MemoryQuality, "usage": &backup.Envelope.Usage, "trace": &backup.Envelope.Traces}
		for kind, target := range targets {
			values, err := r.ReadRecords(ctx, tx, scope, id, kind, 0, "")
			if err != nil {
				return err
			}
			for _, row := range values {
				delete(row, "record_id")
				delete(row, "record_key")
			}
			*target = values
		}
		rows, err := tx.Query(ctx, "SELECT version,payload,change_summary FROM project_brief_versions WHERE workspace_id=$1 AND project_id=$2 ORDER BY version", scope, id)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var version int
			var body []byte
			var summary string
			if err := rows.Scan(&version, &body, &summary); err != nil {
				return err
			}
			var payload any
			if err := json.Unmarshal(body, &payload); err != nil {
				return err
			}
			backup.BriefVersions = append(backup.BriefVersions, map[string]any{"version": version, "brief": payload, "change_summary": summary})
		}
		return rows.Err()
	})
	if err != nil {
		return backup, err
	}
	backup.SHA256 = transfer.Hash(backup)
	if int64(len(jsonBytes(backup))) > 32<<20 {
		return backup, r.Invalid
	}
	return backup, nil
}

func remapEnvelope(envelope *execution.Envelope, id string) {
	for kind, rows := range envelope.Groups() {
		for index, row := range rows {
			row["project_id"] = id
			if text(row["id"]) == "" {
				identity := text(row["candidate_id"])
				if identity == "" {
					identity = uuid.NewSHA1(uuid.NameSpaceURL, []byte(fmt.Sprintf("import:%s:%s:%d", id, kind, index))).String()
				}
				row["id"] = identity
			}
			if row["block_index"] == nil {
				row["block_index"] = task.Integer(row["content"])
			}
			if task.Integer(row["version_number"]) < 1 {
				version := task.Integer(row["version"])
				if version < 1 {
					version = index + 1
				}
				row["version_number"] = version
			}
		}
	}
}

type Transfer = transfer.Transfer

func (r Repository) QueueTransfer(ctx context.Context, scope, id, project, format string, pending []byte) error {
	if r.Audit == nil {
		return errors.New("project transfer audit writer unavailable")
	}
	return r.within(ctx, scope, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "INSERT INTO project_transfers(id,workspace_id,project_id,status,format,result) VALUES($1,$2,$3,'queued',$4,$5)", id, scope, project, format, pending); err != nil {
			return err
		}
		return r.Audit(ctx, tx, scope, "project.export.queued", project)
	})
}
func (r Repository) ClaimTransfer(ctx context.Context, scope, id string) (Transfer, bool, error) {
	var value Transfer
	started := false
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		err := tx.QueryRow(ctx, "UPDATE project_transfers SET status='running',updated_at=now() WHERE workspace_id=$1 AND id=$2 AND status='queued' RETURNING result,format,updated_at", scope, id).Scan(&value.Body, &value.Format, &value.Claimed)
		started = err == nil
		if errors.Is(err, pgx.ErrNoRows) {
			return nil
		}
		return err
	})
	return value, started, err
}
func (r Repository) FinishTransfer(ctx context.Context, scope, id string, claimed time.Time, body []byte, failed bool) error {
	return r.within(ctx, scope, func(tx pgx.Tx) error {
		if failed {
			_, err := tx.Exec(ctx, "UPDATE project_transfers SET status='failed',error='export failed',result=NULL,updated_at=now() WHERE workspace_id=$1 AND id=$2 AND status='running' AND updated_at=$3", scope, id, claimed)
			return err
		}
		_, err := tx.Exec(ctx, "UPDATE project_transfers SET status='completed',result=$1,content_hash=$2,updated_at=now() WHERE workspace_id=$3 AND id=$4 AND status='running' AND updated_at=$5", body, hash(body), scope, id, claimed)
		return err
	})
}
func (r Repository) Transfer(ctx context.Context, scope, id string) (Transfer, error) {
	var value Transfer
	err := r.within(ctx, scope, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT project_id,status,format,COALESCE(content_hash,''),error,result FROM project_transfers WHERE workspace_id=$1 AND id=$2", scope, id).Scan(&value.ProjectID, &value.Status, &value.Format, &value.Hash, &value.Failure, &value.Body)
	})
	return value, err
}
func (r Repository) CancelTransfer(ctx context.Context, scope, id string) error {
	return r.within(ctx, scope, func(tx pgx.Tx) error {
		tag, err := tx.Exec(ctx, "UPDATE project_transfers SET status='cancelled',result=NULL,updated_at=now() WHERE workspace_id=$1 AND id=$2 AND status IN ('queued','running','cancelled')", scope, id)
		if err == nil && tag.RowsAffected() == 0 {
			return r.Conflict
		}
		return err
	})
}
func (r Repository) PendingTransfers(ctx context.Context) (map[string][]string, error) {
	pending := map[string][]string{}
	if r.Pool == nil {
		return nil, errors.New("project transfer database unavailable")
	}
	rows, err := r.Pool.Query(ctx, "SELECT id FROM workspaces ORDER BY id")
	if err != nil {
		return nil, err
	}
	scopes := []string{}
	for rows.Next() {
		var scope string
		if err = rows.Scan(&scope); err != nil {
			rows.Close()
			return nil, err
		}
		scopes = append(scopes, scope)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	for _, scope := range scopes {
		err = r.within(ctx, scope, func(tx pgx.Tx) error {
			if _, err := tx.Exec(ctx, "UPDATE project_transfers SET status='queued',updated_at=now() WHERE workspace_id=$1 AND status='running' AND updated_at<now()-interval '60 seconds'", scope); err != nil {
				return err
			}
			rows, err := tx.Query(ctx, "SELECT id FROM project_transfers WHERE workspace_id=$1 AND status='queued' ORDER BY created_at LIMIT 100", scope)
			if err != nil {
				return err
			}
			defer rows.Close()
			for rows.Next() {
				var id string
				if err = rows.Scan(&id); err != nil {
					return err
				}
				pending[scope] = append(pending[scope], id)
			}
			return rows.Err()
		})
		if err != nil {
			return nil, err
		}
	}
	return pending, nil
}
