package postgres

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/execution"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/task"
)

type ContentRecordWriter = func(context.Context, pgx.Tx, string, string, string, map[string]any, error, error) error

func MarkCandidatesStale(ctx context.Context, tx pgx.Tx, workspace, projectID string) (int64, error) {
	tag, err := tx.Exec(ctx, "UPDATE project_records SET stale=true WHERE workspace_id=$1 AND project_id=$2 AND kind='candidate' AND NOT stale", workspace, projectID)
	return tag.RowsAffected(), err
}

func CandidateIsStale(ctx context.Context, tx pgx.Tx, workspace, projectID, candidateID string) (bool, error) {
	var stale bool
	err := tx.QueryRow(ctx, "SELECT stale FROM project_records WHERE workspace_id=$1 AND project_id=$2 AND kind='candidate' AND (record_key=$3 OR payload->>'id'=$3 OR payload->>'candidate_id'=$3)", workspace, projectID, candidateID).Scan(&stale)
	return stale, err
}

func PersistEnvelope(ctx context.Context, tx pgx.Tx, scope, id, job, stateHash string, envelope execution.Envelope, invalid, conflict error, writeContent ContentRecordWriter) error {
	if stateHash == "" {
		stateHash = hash(jsonBytes(envelope.State))
	}
	if _, err := tx.Exec(ctx, "UPDATE project_projections SET state=$1,state_hash=$2,revision=revision+1,updated_at=now() WHERE workspace_id=$3 AND id=$4", jsonBytes(envelope.State), stateHash, scope, id); err != nil {
		return err
	}
	for kind, records := range envelope.Groups() {
		for _, record := range records {
			if err := InsertRecord(ctx, tx, scope, id, job, kind, record, invalid, conflict, writeContent); err != nil {
				return err
			}
		}
	}
	return nil
}
func InsertRecord(ctx context.Context, tx pgx.Tx, scope, id, job, kind string, row map[string]any, invalid, conflict error, writeContent ContentRecordWriter) error {
	if kind == "content" {
		if writeContent == nil {
			return errors.New("content record writer unavailable")
		}
		return writeContent(ctx, tx, scope, id, job, row, invalid, conflict)
	}
	chapter := task.Integer(row["block_index"])
	if chapter == 0 {
		chapter = task.Integer(row["content"])
	}
	version := task.Integer(row["version_number"])
	if version == 0 {
		version = task.Integer(row["version"])
	}
	artifact := text(row["artifact_type"])
	key := ""
	for _, field := range []string{"id", "candidate_id", "evaluation_id", "snapshot_id", "event_id"} {
		if s := text(row[field]); s != "" {
			key = s
			break
		}
	}
	stale, _ := row["stale"].(bool)
	baseline, _ := row["baseline"].(bool)
	payload := map[string]any{}
	for field, value := range row {
		if field != "record_id" && field != "record_key" && field != "stale" && field != "baseline" {
			payload[field] = value
		}
	}
	body := jsonBytes(payload)
	hash := hash(body)
	if key == "" {
		if kind == "content" || kind == "planning" {
			key = fmt.Sprintf("%s:%d:%d", artifact, chapter, version)
		} else {
			key = hash
		}
	}
	var source any
	if job != "" {
		source = job
	}
	recordID := uuid.NewSHA1(uuid.NameSpaceURL, []byte(scope+":"+id+":"+kind+":"+key)).String()
	tag, err := tx.Exec(ctx, `INSERT INTO project_records(id,workspace_id,project_id,kind,record_key,block_index,version_number,artifact_type,payload,content_hash,source_job_id,stale,baseline)
 VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) ON CONFLICT(project_id,kind,record_key) DO NOTHING`, recordID, scope, id, kind, key, chapter, version, artifact, body, hash, source, stale, baseline)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		var prior []byte
		if err = tx.QueryRow(ctx, "SELECT payload FROM project_records WHERE workspace_id=$1 AND project_id=$2 AND kind=$3 AND record_key=$4", scope, id, kind, key).Scan(&prior); err != nil {
			return err
		}
		if !equalJSON(prior, body) {
			return conflict
		}
	}
	return nil
}
func ReadRecords(ctx context.Context, tx pgx.Tx, scope, id, kind string, chapter int, artifact string) ([]map[string]any, error) {
	table := "project_records"
	if kind == "content" {
		table = "project_content_versions"
	}
	rows, err := tx.Query(ctx, `SELECT id,record_key,payload,stale,baseline FROM `+table+` WHERE workspace_id=$1 AND project_id=$2 AND kind=$3 AND ($4=0 OR block_index=$4) AND ($5='' OR artifact_type=$5) ORDER BY version_number DESC,created_at DESC`, scope, id, kind, chapter, artifact)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	values := []map[string]any{}
	for rows.Next() {
		var body []byte
		var recordID, key string
		var stale, baseline bool
		if err = rows.Scan(&recordID, &key, &body, &stale, &baseline); err != nil {
			return nil, err
		}
		var value map[string]any
		if err = json.Unmarshal(body, &value); err != nil {
			return nil, err
		}
		value["record_id"] = recordID
		value["record_key"] = key
		if kind == "candidate" {
			value["stale"] = stale
		}
		if kind == "evaluation" {
			value["baseline"] = baseline
		}
		values = append(values, value)
	}
	return values, rows.Err()
}
func FindVersion(ctx context.Context, tx pgx.Tx, scope, id, kind string, chapter, version int, artifact string) (map[string]any, error) {
	var body []byte
	table := "project_records"
	if kind == "content" {
		table = "project_content_versions"
	}
	err := tx.QueryRow(ctx, "SELECT payload FROM "+table+" WHERE workspace_id=$1 AND project_id=$2 AND kind=$3 AND block_index=$4 AND version_number=$5 AND artifact_type=$6", scope, id, kind, chapter, version, artifact).Scan(&body)
	if err != nil {
		return nil, err
	}
	var result map[string]any
	err = json.Unmarshal(body, &result)
	return result, err
}

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
