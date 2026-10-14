// Package postgres coordinates durable task requests and their transactional outbox.
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
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/go-control-plane/internal/contracts"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/execution"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/governance"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/project"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/task"
	"github.com/sakuya/novel-agent/go-control-plane/internal/platform/database"
)

type Outbox interface {
	SubmitWithin(context.Context, pgx.Tx, contracts.Command) error
}
type Adapter struct {
	Pool                  *pgxpool.Pool
	Outbox                Outbox
	Invalid, Conflict     error
	Freezer, InputFreezer func(context.Context, string, map[string]any) (map[string]any, error)
	Snapshot              func(project.Resource, map[string]any, string) map[string]any
	LoadProject           func(context.Context, pgx.Tx, string, string, bool) (project.Resource, error)
	NextDocumentVersion   func(context.Context, pgx.Tx, string, string) (int, error)
	AttachSnapshot        func(context.Context, pgx.Tx, contracts.Command, map[string]any) (contracts.Command, error)
	EnsureIdle            func(context.Context, pgx.Tx, string, string) error
	Validate              func(context.Context, pgx.Tx, string, project.Resource, string, map[string]any) error
	Envelope              func(context.Context, pgx.Tx, string, project.Resource) (execution.Envelope, error)
	Audit                 func(context.Context, pgx.Tx, string, string, string) error
}

func (a Adapter) SubmitCommand(ctx context.Context, scope string, resource project.Resource, operation string, args map[string]any, key string) (contracts.Command, error) {
	if err := task.Validate(operation, args); err != nil {
		return contracts.Command{}, a.Invalid
	}
	if len(key) > 200 {
		return contracts.Command{}, a.Invalid
	}
	if key == "" {
		key = uuid.NewString()
	}
	key = "project:" + resource.ID + ":" + operation + ":" + key
	if len(key) > 200 {
		key = hash([]byte(key))
	}
	id := uuid.NewSHA1(uuid.NameSpaceURL, []byte("command:"+scope+":"+key)).String()
	job := uuid.NewSHA1(uuid.NameSpaceURL, []byte("job:"+id)).String()
	var replay *contracts.Command
	if operation == "candidates" && args["count"] == nil {
		args["count"] = 3
	}
	if (operation == "memory_rebuild" || operation == "memory_evaluate") && args["k"] == nil {
		args["k"] = 5
	}
	definition, _ := resource.Input["input"].(map[string]any)
	objective := text(definition["objective"])
	if objective == "" {
		objective = text(definition["inspiration"])
	}
	nodes := []string{"plan", "write", "review"}
	payload := map[string]any{"project_id": resource.ID, "document_id": resource.DocumentID, "document_type": resource.ContentType, "content_type": resource.ContentType, "title": resource.Title, "objective": objective, "nodes": nodes, "input": resource.Input["input"], "brief": resource.Input["brief"], "brief_version": resource.BriefVersion, "operation": operation, "arguments": args, "version": 1}
	if len(resource.State) > 0 {
		payload["imported_state"] = resource.State
	}
	err := a.within(ctx, scope, func(tx pgx.Tx) error {
		var body []byte
		err := tx.QueryRow(ctx, "SELECT payload FROM command_outbox WHERE workspace_id=$1 AND command_id=$2", scope, id).Scan(&body)
		if err == nil {
			var prior contracts.Command
			if err = json.Unmarshal(body, &prior); err != nil {
				return err
			}
			var previous map[string]any
			if err = json.Unmarshal(prior.Payload, &previous); err != nil {
				return err
			}
			if !equalJSON(fingerprint(payload), fingerprint(previous)) {
				return a.Conflict
			}
			replay = &prior
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		n, err := a.loadProject(ctx, tx, scope, resource.ID, true)
		if err != nil {
			return err
		}
		if n.Revision != resource.Revision {
			return a.Conflict
		}
		if err = a.EnsureIdle(ctx, tx, scope, n.ID); err != nil {
			return err
		}
		if err = a.Validate(ctx, tx, scope, n, operation, args); err != nil {
			return err
		}
		version, versionErr := a.nextDocumentVersion(ctx, tx, scope, n.DocumentID)
		if versionErr != nil {
			return versionErr
		}
		payload["version"] = version
		return nil
	})
	if err != nil {
		return contracts.Command{}, err
	}
	if replay != nil {
		return *replay, nil
	}
	if a.InputFreezer != nil {
		freezeInput := map[string]any{}
		for key, value := range definition {
			freezeInput[key] = value
		}
		freezeInput["brief"] = resource.Input["brief"]
		input, freezeErr := a.InputFreezer(ctx, scope, freezeInput)
		err = freezeErr
		if err != nil {
			return contracts.Command{}, err
		}
		payload["frozen_resources"] = input
	}
	workflow := project.Workflow(resource.ContentType)
	if name := text(args["workflow"]); name != "" {
		version := task.Integer(args["workflow_version"])
		if version < 1 {
			return contracts.Command{}, a.Invalid
		}
		workflow = fmt.Sprintf("%s@%d", name, version)
	}
	snapshot := a.Snapshot(resource, args, workflow)
	if a.Freezer != nil {
		snapshot, err = a.Freezer(ctx, scope, snapshot)
		if err != nil {
			return contracts.Command{}, err
		}
	}
	canonical, err := governance.CanonicalJSON(snapshot)
	if err != nil {
		return contracts.Command{}, err
	}
	hash := hash(canonical)
	snapshot["snapshot_hash"] = hash
	command := contracts.Command{SchemaVersion: "1.0", ID: id, Type: "workflow.run", WorkspaceID: scope, ProjectID: resource.ID, JobID: job, WorkflowRunID: job, CapabilitySnapshotHash: hash, TraceID: id, IdempotencyKey: key, Payload: jsonBytes(payload)}
	if a.Outbox == nil {
		return contracts.Command{}, errors.New("messaging unavailable")
	}
	err = a.within(ctx, scope, func(tx pgx.Tx) error {
		n, err := a.loadProject(ctx, tx, scope, resource.ID, true)
		if err != nil {
			return err
		}
		if n.Revision != resource.Revision {
			return a.Conflict
		}
		var previous []byte
		err = tx.QueryRow(ctx, "SELECT payload FROM command_outbox WHERE workspace_id=$1 AND command_id=$2", scope, id).Scan(&previous)
		if err == nil {
			var prior contracts.Command
			if err = json.Unmarshal(previous, &prior); err != nil {
				return err
			}
			var priorPayload map[string]any
			if err = json.Unmarshal(prior.Payload, &priorPayload); err != nil {
				return err
			}
			if !equalJSON(fingerprint(payload), fingerprint(priorPayload)) {
				return a.Conflict
			}
			command = prior
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if err = a.EnsureIdle(ctx, tx, scope, resource.ID); err != nil {
			return err
		}
		if err = a.Validate(ctx, tx, scope, n, operation, args); err != nil {
			return err
		}
		if len(n.State) > 0 {
			envelope, err := a.Envelope(ctx, tx, scope, n)
			if err != nil {
				return err
			}
			payload["imported_envelope"] = envelope
		}
		version, versionErr := a.nextDocumentVersion(ctx, tx, scope, n.DocumentID)
		if versionErr != nil {
			return versionErr
		}
		payload["version"] = version
		command.Payload = jsonBytes(payload)
		if _, err = tx.Exec(ctx, "INSERT INTO capability_snapshots(workspace_id,content_hash,snapshot) VALUES($1,$2,$3) ON CONFLICT(workspace_id,content_hash) DO NOTHING", scope, hash, jsonBytes(snapshot)); err != nil {
			return err
		}
		command, err = a.AttachSnapshot(ctx, tx, command, snapshot)
		if err != nil {
			return err
		}
		if err = a.Outbox.SubmitWithin(ctx, tx, command); err != nil {
			return err
		}
		return a.Audit(ctx, tx, scope, "project.operation."+operation, resource.ID)
	})
	return command, err
}
func fingerprint(payload map[string]any) []byte {
	copy := map[string]any{}
	for key, value := range payload {
		if key != "version" && key != "imported_state" && key != "imported_envelope" && key != "frozen_resources" {
			copy[key] = value
		}
	}
	return jsonBytes(copy)
}

func (a Adapter) loadProject(ctx context.Context, tx pgx.Tx, scope, id string, lock bool) (project.Resource, error) {
	if a.LoadProject == nil {
		return project.Resource{}, errors.New("project reader unavailable")
	}
	return a.LoadProject(ctx, tx, scope, id, lock)
}

func (a Adapter) nextDocumentVersion(ctx context.Context, tx pgx.Tx, workspace, document string) (int, error) {
	if a.NextDocumentVersion == nil {
		return 0, errors.New("content version reader unavailable")
	}
	return a.NextDocumentVersion(ctx, tx, workspace, document)
}

func (a Adapter) within(ctx context.Context, scope string, fn func(pgx.Tx) error) error {
	if a.Pool == nil {
		return errors.New("task database unavailable")
	}
	tx, err := database.BeginWorkspaceTx(ctx, a.Pool, scope)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	if err = fn(tx); err != nil {
		return err
	}
	return tx.Commit(ctx)
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
