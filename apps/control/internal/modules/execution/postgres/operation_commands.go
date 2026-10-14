package postgres

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/sakuya/ai-content-platform/apps/control/internal/contracts"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/governance"
)

func (a *MQRepository) SubmitOperation(ctx context.Context, workspace, project, operation, key string, arguments map[string]any) (contracts.Command, error) {
	if a.pool == nil {
		return contracts.Command{}, errors.New("operation messaging unavailable")
	}
	commandID := uuid.NewSHA1(uuid.NameSpaceURL, []byte("command:"+workspace+":"+key)).String()
	jobID := uuid.NewSHA1(uuid.NameSpaceURL, []byte("job:"+commandID)).String()
	request := GenerationRequest{ProjectID: project, IdempotencyKey: key, RuntimeOperation: operation, Arguments: arguments}
	requestBody, err := json.Marshal(request)
	if err != nil {
		return contracts.Command{}, err
	}
	command := contracts.Command{SchemaVersion: "1.0", ID: commandID, Type: "workflow.run", WorkspaceID: workspace, ProjectID: project, JobID: jobID, WorkflowRunID: jobID, TraceID: commandID, IdempotencyKey: key, Payload: requestBody}
	err = a.within(ctx, workspace, func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", workspace+":"+key); err != nil {
			return err
		}
		var previous []byte
		err := tx.QueryRow(ctx, "SELECT payload FROM command_outbox WHERE command_id=$1 AND workspace_id=$2", commandID, workspace).Scan(&previous)
		if err == nil {
			var prior contracts.Command
			if err = json.Unmarshal(previous, &prior); err != nil {
				return err
			}
			if prior.ProjectID != project || !sameJSON(prior.Payload, requestBody) {
				return ErrMessageConflict
			}
			command = prior
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		if err = operationProject(ctx, tx, workspace, project); err != nil {
			return err
		}
		if baseline, ok := arguments["baseline_run_id"].(string); operation == "benchmark" && ok && baseline != "" {
			if a.benchmarkReportResolver == nil {
				return errors.New("evaluation benchmark resolver unavailable")
			}
			data, readErr := a.benchmarkReportResolver(ctx, tx, workspace, baseline)
			if readErr != nil {
				return readErr
			}
			var value any
			if err = json.Unmarshal(data, &value); err != nil {
				return err
			}
			// The referenced baseline is resolved once and frozen in the execution bundle.
			resolved := map[string]any{}
			for key, value := range arguments {
				resolved[key] = value
			}
			resolved["baseline"] = value
			request.Arguments = resolved
		}
		workflow := "memory.refresh@1"
		if operation == "benchmark" {
			workflow = "content.generate@1"
		}
		snapshot := generationSnapshot(request, workflow)
		if a.snapshotFreezer != nil {
			snapshot, err = a.snapshotFreezer(ctx, workspace, snapshot)
			if err != nil {
				return err
			}
		}
		encoded, err := governance.CanonicalJSON(snapshot)
		if err != nil {
			return err
		}
		hash := resourceHash(encoded)
		snapshot["snapshot_hash"] = hash
		encoded, err = json.Marshal(snapshot)
		if err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, "INSERT INTO capability_snapshots(workspace_id,content_hash,snapshot) VALUES($1,$2,$3) ON CONFLICT(workspace_id,content_hash) DO NOTHING", workspace, hash, encoded); err != nil {
			return err
		}
		command.CapabilitySnapshotHash = hash
		// Preserve the original request in the Outbox for stable idempotency comparisons.
		executionCommand := command
		executionCommand.Payload, err = json.Marshal(request)
		if err != nil {
			return err
		}
		executionCommand, err = a.AttachExecutionSnapshot(ctx, tx, executionCommand, snapshot)
		if err != nil {
			return err
		}
		command.ExecutionSnapshot = executionCommand.ExecutionSnapshot
		if command.ExecutionSnapshot == nil {
			return errors.New("immutable execution snapshot storage is required")
		}
		return a.SubmitWithin(ctx, tx, command)
	})
	return command, err
}

// ProjectOperationEvent is called only for a completion accepted by the job state projection.
func ProjectOperationEvent(ctx context.Context, tx pgx.Tx, event contracts.Event, writeBenchmark BenchmarkWriter) error {
	if event.Type != "job.completed" {
		return nil
	}
	var commandBody []byte
	if err := tx.QueryRow(ctx, "SELECT request FROM jobs WHERE id=$1 AND workspace_id=$2 AND project_id=$3", event.JobID, event.WorkspaceID, event.ProjectID).Scan(&commandBody); err != nil {
		return err
	}
	var command contracts.Command
	if err := json.Unmarshal(commandBody, &command); err != nil {
		return err
	}
	var request GenerationRequest
	if err := json.Unmarshal(command.Payload, &request); err != nil {
		return err
	}
	if request.RuntimeOperation == "" {
		return nil
	}
	var outcome struct {
		Operation string          `json:"operation"`
		Result    json.RawMessage `json:"operation_result"`
	}
	if err := json.Unmarshal(event.Payload, &outcome); err != nil {
		return err
	}
	if outcome.Operation != request.RuntimeOperation || len(outcome.Result) == 0 || string(outcome.Result) == "null" {
		return ErrMessageConflict
	}
	if err := storeOperationProjection(ctx, tx, event, "runtime_operation", event.JobID, outcome.Result, false); err != nil {
		return err
	}
	switch outcome.Operation {
	case "memory.refresh":
		var result map[string]json.RawMessage
		if err := json.Unmarshal(outcome.Result, &result); err != nil {
			return err
		}
		for _, kind := range []string{"memory", "usage", "traces"} {
			if data, ok := result[kind]; ok {
				if err := storeOperationProjection(ctx, tx, event, "runtime_"+kind, event.JobID, data, false); err != nil {
					return err
				}
			}
		}
	case "knowledge.publish":
		var pack map[string]any
		if err := json.Unmarshal(outcome.Result, &pack); err != nil {
			return err
		}
		id, _ := pack["id"].(string)
		if _, err := uuid.Parse(id); err != nil {
			return ErrMessageConflict
		}
		return storeOperationProjection(ctx, tx, event, "knowledge_pack", id, outcome.Result, false)
	case "knowledge.bind", "knowledge.unbind":
		pack, _ := request.Arguments["pack_id"].(string)
		if _, err := uuid.Parse(pack); err != nil {
			return ErrMessageConflict
		}
		payload, err := json.Marshal(map[string]any{"pack_id": pack, "project_id": event.ProjectID, "shared": outcome.Operation == "knowledge.bind"})
		if err != nil {
			return err
		}
		return storeOperationProjection(ctx, tx, event, "knowledge_binding", pack, payload, outcome.Operation == "knowledge.unbind")
	case "benchmark":
		var report map[string]any
		if err := json.Unmarshal(outcome.Result, &report); err != nil {
			return err
		}
		if report == nil {
			return ErrMessageConflict
		}
		report["id"] = event.JobID
		data, err := json.Marshal(report)
		if err != nil {
			return err
		}
		if writeBenchmark == nil {
			return errors.New("evaluation benchmark writer unavailable")
		}
		return writeBenchmark(ctx, tx, event.WorkspaceID, event.JobID, data)
	}
	return nil
}

func storeOperationProjection(ctx context.Context, tx pgx.Tx, event contracts.Event, kind, key string, data json.RawMessage, stale bool) error {
	_, err := tx.Exec(ctx, "INSERT INTO project_records(workspace_id,project_id,kind,record_key,payload,content_hash,source_job_id,stale) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(project_id,kind,record_key) DO UPDATE SET payload=EXCLUDED.payload,content_hash=EXCLUDED.content_hash,source_job_id=EXCLUDED.source_job_id,stale=EXCLUDED.stale,created_at=now()", event.WorkspaceID, event.ProjectID, kind, key, data, resourceHash(data), event.JobID, stale)
	return err
}
