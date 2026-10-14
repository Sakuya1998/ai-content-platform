package postgres

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/ai-content-platform/apps/control/internal/contracts"
	contentmodule "github.com/sakuya/ai-content-platform/apps/control/internal/modules/content"
	taskmodule "github.com/sakuya/ai-content-platform/apps/control/internal/modules/task"
	"github.com/sakuya/ai-content-platform/apps/control/internal/platform/config"
	"github.com/sakuya/ai-content-platform/apps/control/internal/platform/database"
)

var ErrMessageConflict = taskmodule.ErrConflict

type MQRepository struct {
	pool                      *pgxpool.Pool
	topics                    config.MQTopics
	contentArtifacts          contentReader
	executionSnapshotStore    executionSnapshotWriter
	executionResourceResolver executionResourceResolver
	projectProjector          func(context.Context, pgx.Tx, contracts.Event) error
	snapshotFreezer           SnapshotFreezer
	frozenModelProfile        FrozenModelProfileReader
	benchmarkWriter           BenchmarkWriter
	benchmarkReader           BenchmarkReader
	benchmarkReportResolver   BenchmarkReportResolver
	prepareContentDocument    ContentDocumentPreparer
	commitContentVersion      ContentVersionCommitter
	contentVersionReader      ContentVersionReader
	contentVersionIDByJob     ContentVersionIDByJobReader
}

type RepositorySettings struct {
	ContentArtifacts       contentReader
	SnapshotStore          executionSnapshotWriter
	ResourceResolver       executionResourceResolver
	Projector              func(context.Context, pgx.Tx, contracts.Event) error
	SnapshotFreezer        SnapshotFreezer
	FrozenModelProfile     FrozenModelProfileReader
	BenchmarkWriter        BenchmarkWriter
	BenchmarkReader        BenchmarkReader
	BenchmarkReportResolver BenchmarkReportResolver
	PrepareContentDocument ContentDocumentPreparer
	CommitContentVersion   ContentVersionCommitter
	ReadContentVersion     ContentVersionReader
	VersionIDBySourceJob   ContentVersionIDByJobReader
}

type FrozenModelProfileReader = func(context.Context, pgx.Tx, string, string) (ModelProfile, error)
type BenchmarkWriter = func(context.Context, pgx.Tx, string, string, json.RawMessage) error
type BenchmarkReader = func(context.Context, pgx.Tx, string, string) (json.RawMessage, error)
type BenchmarkReportResolver = func(context.Context, pgx.Tx, string, string) (json.RawMessage, error)
type ContentDocumentPreparer = func(context.Context, pgx.Tx, string, string, string, string, string) (int, error)
type ContentVersionCommitter = func(context.Context, pgx.Tx, contentmodule.GeneratedVersionCommit) error
type ContentVersionReader = func(context.Context, pgx.Tx, string, string, string) (contentmodule.ContentVersion, error)
type ContentVersionIDByJobReader = func(context.Context, pgx.Tx, string, string) (string, error)

type JobProjection struct {
	ID       string          `json:"job_id"`
	Status   string          `json:"status"`
	Sequence int64           `json:"sequence"`
	Result   json.RawMessage `json:"result"`
}

func NewMQRepository(pool *pgxpool.Pool, topics config.MQTopics, settings ...RepositorySettings) *MQRepository {
	var cfg RepositorySettings
	if len(settings) > 0 {
		cfg = settings[0]
	}
	return &MQRepository{pool: pool, topics: topics, contentArtifacts: cfg.ContentArtifacts,
		executionSnapshotStore: cfg.SnapshotStore, executionResourceResolver: cfg.ResourceResolver,
		projectProjector: cfg.Projector, snapshotFreezer: cfg.SnapshotFreezer,
		frozenModelProfile: cfg.FrozenModelProfile, benchmarkWriter: cfg.BenchmarkWriter,
		benchmarkReader: cfg.BenchmarkReader, benchmarkReportResolver: cfg.BenchmarkReportResolver,
		prepareContentDocument: cfg.PrepareContentDocument,
		commitContentVersion: cfg.CommitContentVersion, contentVersionReader: cfg.ReadContentVersion,
		contentVersionIDByJob: cfg.VersionIDBySourceJob}
}

func (r *MQRepository) WithFrozenModelProfileReader(reader FrozenModelProfileReader) *MQRepository {
	r.frozenModelProfile = reader
	return r
}
func (r *MQRepository) within(ctx context.Context, workspace string, fn func(pgx.Tx) error) error {
	tx, err := database.BeginWorkspaceTx(ctx, r.pool, workspace)
	if err != nil {
		return err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (r *MQRepository) Submit(ctx context.Context, command contracts.Command) error {
	return r.within(ctx, command.WorkspaceID, func(tx pgx.Tx) error { return r.SubmitWithin(ctx, tx, command) })
}

func (r *MQRepository) SubmitWithin(ctx context.Context, tx pgx.Tx, command contracts.Command) error {
	body, err := json.Marshal(command)
	if err != nil {
		return err
	}
	return func() error {
		if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", command.WorkspaceID+":"+command.IdempotencyKey); err != nil {
			return err
		}
		var exists bool
		if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM projects WHERE id=$1 AND workspace_id=$2)", command.ProjectID, command.WorkspaceID).Scan(&exists); err != nil {
			return err
		}
		if !exists {
			return ErrNotFound
		}
		var previous []byte
		err := tx.QueryRow(ctx, "SELECT payload FROM command_outbox WHERE command_id=$1", command.ID).Scan(&previous)
		if err == nil {
			if !sameJSON(previous, body) {
				return ErrMessageConflict
			}
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		topic := r.topics.Tasks
		if command.Type == "workflow.run" || command.Type == "node.run" {
			var duplicate bool
			if err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM jobs WHERE workspace_id=$1 AND request->>'idempotency_key'=$2)", command.WorkspaceID, command.IdempotencyKey).Scan(&duplicate); err != nil {
				return err
			}
			if duplicate {
				return ErrMessageConflict
			}
			_, err = tx.Exec(ctx, `INSERT INTO jobs(id,workspace_id,project_id,workflow_template_version,status,request)
                VALUES($1,$2,$3,$4,'queued',$5)`, command.JobID, command.WorkspaceID, command.ProjectID, command.Type, body)
			if err != nil {
				return err
			}
		} else {
			topic = r.topics.Control
			var status, project string
			err = tx.QueryRow(ctx, "SELECT status,project_id FROM jobs WHERE id=$1 FOR UPDATE", command.JobID).Scan(&status, &project)
			if errors.Is(err, pgx.ErrNoRows) {
				return ErrNotFound
			}
			if err != nil {
				return err
			}
			if project != command.ProjectID {
				return ErrMessageConflict
			}
			if command.Type == "job.cancel" {
				if status == "completed" || status == "failed" || status == "cancelled" {
					return ErrMessageConflict
				}
				_, err = tx.Exec(ctx, "UPDATE jobs SET status='cancel_requested',updated_at=now() WHERE id=$1", command.JobID)
			} else if command.Type == "job.resume" && status == "waiting_review" {
				_, err = tx.Exec(ctx, "UPDATE jobs SET status='queued',updated_at=now() WHERE id=$1", command.JobID)
			} else {
				return ErrMessageConflict
			}
			if err != nil {
				return err
			}
		}
		_, err = tx.Exec(ctx, "INSERT INTO command_outbox(command_id,workspace_id,job_id,topic,payload) VALUES($1,$2,$3,$4,$5)", command.ID, command.WorkspaceID, command.JobID, topic, body)
		return err
	}()
}

func (r *MQRepository) Scopes(ctx context.Context) ([]string, error) {
	rows, err := r.pool.Query(ctx, "SELECT id FROM workspaces ORDER BY id")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var scopes []string
	for rows.Next() {
		var scope string
		if err = rows.Scan(&scope); err != nil {
			return nil, err
		}
		scopes = append(scopes, scope)
	}
	return scopes, rows.Err()
}

func (r *MQRepository) Relay(ctx context.Context, workspace string, publish func(context.Context, string, string, string, []byte) error) error {
	return r.within(ctx, workspace, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, "SELECT id,command_id,topic,payload FROM command_outbox WHERE published_at IS NULL AND next_attempt_at<=now() ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 16")
		if err != nil {
			return err
		}
		type pending struct {
			id         int64
			key, topic string
			body       []byte
		}
		var messages []pending
		for rows.Next() {
			var m pending
			if err = rows.Scan(&m.id, &m.key, &m.topic, &m.body); err != nil {
				rows.Close()
				return err
			}
			messages = append(messages, m)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for _, message := range messages {
			var command contracts.Command
			if err = json.Unmarshal(message.body, &command); err != nil {
				return err
			}
			wire, wireErr := commandMessage(command)
			if wireErr != nil {
				return wireErr
			}
			if err = publish(ctx, message.topic, message.key, command.Type, wire); err != nil {
				if _, err = tx.Exec(ctx, "UPDATE command_outbox SET attempts=attempts+1,next_attempt_at=now()+interval '5 seconds' WHERE id=$1", message.id); err != nil {
					return err
				}
				continue
			}
			if _, err = tx.Exec(ctx, "UPDATE command_outbox SET published_at=now(),attempts=attempts+1 WHERE id=$1", message.id); err != nil {
				return err
			}
		}
		return nil
	})
}

func commandMessage(command contracts.Command) ([]byte, error) {
	if command.Type == "workflow.run" || command.Type == "node.run" {
		if command.ExecutionSnapshot == nil {
			return nil, errors.New("execution snapshot reference is required for MQ publication")
		}
		command.Payload = json.RawMessage(`{}`)
	}
	return json.Marshal(command)
}

func (r *MQRepository) Get(ctx context.Context, workspace, id string) (JobProjection, error) {
	var job JobProjection
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT id,status,last_event_sequence,result FROM jobs WHERE id=$1", id).Scan(&job.ID, &job.Status, &job.Sequence, &job.Result)
	})
	if errors.Is(err, pgx.ErrNoRows) {
		err = ErrNotFound
	}
	return job, err
}

func (r *MQRepository) Project(ctx context.Context, body []byte) error {
	var event contracts.Event
	if err := json.Unmarshal(body, &event); err != nil {
		return err
	}
	if event.SchemaVersion != "1.0" || event.ID == "" || event.Sequence < 1 || event.ProjectID == "" || event.JobID == "" {
		return ErrMessageConflict
	}
	return r.within(ctx, event.WorkspaceID, func(tx pgx.Tx) error {
		var exists bool
		if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM projects WHERE id=$1 AND workspace_id=$2)", event.ProjectID, event.WorkspaceID).Scan(&exists); err != nil {
			return err
		}
		if !exists {
			return ErrNotFound
		}
		var sequence int64
		var status, project string
		var request []byte
		err := tx.QueryRow(ctx, "SELECT last_event_sequence,status,project_id,request FROM jobs WHERE id::text=$1 FOR UPDATE", event.JobID).Scan(&sequence, &status, &project, &request)
		memory := strings.HasPrefix(event.Type, "memory.")
		if errors.Is(err, pgx.ErrNoRows) && memory {
			err = nil
		} else if err == nil && project != event.ProjectID {
			return ErrMessageConflict
		}
		if err != nil {
			return err
		}
		if !memory {
			var command contracts.Command
			if json.Unmarshal(request, &command) != nil || command.WorkflowRunID == "" || event.WorkflowRunID != command.WorkflowRunID {
				return ErrMessageConflict
			}
		}
		var inserted string
		err = tx.QueryRow(ctx, `INSERT INTO runtime_event_inbox(event_id,workspace_id,job_id,sequence,event_type,payload)
            VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT DO NOTHING RETURNING event_id`, event.ID, event.WorkspaceID, event.JobID, event.Sequence, event.Type, body).Scan(&inserted)
		if errors.Is(err, pgx.ErrNoRows) {
			var previous []byte
			if err = tx.QueryRow(ctx, "SELECT payload FROM runtime_event_inbox WHERE event_id=$1", event.ID).Scan(&previous); err != nil {
				return err
			}
			if !sameJSON(previous, body) {
				return ErrMessageConflict
			}
			return nil
		}
		if err != nil {
			return err
		}
		if memory {
			_, err = tx.Exec(ctx, "UPDATE runtime_event_inbox SET applied_at=now() WHERE event_id=$1", event.ID)
			return err
		}
		for {
			var payload []byte
			err = tx.QueryRow(ctx, "SELECT payload FROM runtime_event_inbox WHERE job_id=$1 AND sequence=$2", event.JobID, sequence+1).Scan(&payload)
			if errors.Is(err, pgx.ErrNoRows) {
				break
			}
			if err != nil {
				return err
			}
			var next contracts.Event
			if err = json.Unmarshal(payload, &next); err != nil {
				return err
			}
			terminal := taskmodule.Terminal(status)
			telemetry := next.Type == "project.usage.recorded" || next.Type == "project.trace.recorded"
			if r.projectProjector != nil && strings.HasPrefix(next.Type, "project.") && (!terminal || telemetry) {
				if err = r.projectProjector(ctx, tx, next); err != nil {
					return err
				}
			}
			if next.Type == "content.version.committed" {
				if err = r.commitGenerated(ctx, tx, next); err != nil {
					return err
				}
			}
			outcomeType := ""
			if projected, accepted := taskmodule.ProjectState(status, next.Type); accepted {
				status = projected
				outcomeType = next.Type
			}
			if outcomeType == "job.completed" {
				if err = ProjectOperationEvent(ctx, tx, next, r.benchmarkWriter); err != nil {
					return err
				}
			}
			sequence++
			if _, err = tx.Exec(ctx, "UPDATE jobs SET status=$1,last_event_sequence=$2,state_version=state_version+1,updated_at=now(),result=CASE WHEN $3 IN ('job.completed','job.waiting_review','job.cancel_requested','job.cancelled') THEN $4::jsonb ELSE result END,error=CASE WHEN $3='job.failed' THEN $4::jsonb ELSE error END WHERE id::text=$5", status, sequence, outcomeType, next.Payload, event.JobID); err != nil {
				return err
			}
			if _, err = tx.Exec(ctx, "UPDATE runtime_event_inbox SET applied_at=now() WHERE event_id=$1", next.ID); err != nil {
				return err
			}
		}
		return nil
	})
}

func sameJSON(left, right []byte) bool {
	var a, b any
	if json.Unmarshal(left, &a) != nil || json.Unmarshal(right, &b) != nil {
		return false
	}
	x, _ := json.Marshal(a)
	y, _ := json.Marshal(b)
	return bytes.Equal(x, y)
}
