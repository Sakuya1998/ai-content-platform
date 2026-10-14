package postgres

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/sakuya/novel-agent/apps/control/internal/contracts"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/content"
	executionmodule "github.com/sakuya/novel-agent/apps/control/internal/modules/execution"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/governance"
	productionmodule "github.com/sakuya/novel-agent/apps/control/internal/modules/production"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/objectstorage"
)

type GenerationRequest = executionmodule.Request
type GeneratedBlock = content.GeneratedBlock
type GeneratedDocument = content.GeneratedDocument
type ContentVersion = content.ContentVersion
type ExecutionBundle struct {
	SchemaVersion     string            `json:"schema_version"`
	WorkspaceID       string            `json:"workspace_id"`
	ProjectID         string            `json:"project_id"`
	JobID             string            `json:"job_id"`
	Snapshot          map[string]any    `json:"snapshot"`
	Request           GenerationRequest `json:"request"`
	Source            *ContentVersion   `json:"source"`
	Inputs            []ContentVersion  `json:"inputs"`
	ResolvedResources map[string]any    `json:"resolved_resources"`
	Committed         map[string]any    `json:"committed_version,omitempty"`
}
type contentReader interface {
	Get(context.Context, string, string) ([]byte, error)
}

type executionSnapshotWriter interface {
	Put(context.Context, string, []byte, string) (objectstorage.ArtifactReference, error)
}

type executionResourceResolver func(context.Context, string, map[string]any) (map[string]any, error)

func (r *MQRepository) writeExecutionSnapshot(ctx context.Context, bundle ExecutionBundle) (*contracts.ExecutionSnapshotReference, error) {
	if r.executionSnapshotStore == nil {
		return nil, errors.New("immutable execution snapshot storage is required")
	}
	if bundle.ResolvedResources == nil {
		bundle.ResolvedResources = map[string]any{}
	}
	data, err := json.Marshal(bundle)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256(data)
	hash := hex.EncodeToString(digest[:])
	key, err := objectstorage.ObjectKey(bundle.WorkspaceID, bundle.ProjectID, "assets", bundle.JobID, "execution.json", hash)
	if err != nil {
		return nil, err
	}
	reference, err := r.executionSnapshotStore.Put(ctx, key, data, "application/json")
	if err != nil {
		return nil, err
	}
	if reference.ObjectKey != key || reference.SHA256 != hash || reference.SizeBytes != int64(len(data)) {
		return nil, errors.New("execution snapshot store returned mismatched object metadata")
	}
	return &contracts.ExecutionSnapshotReference{ObjectKey: key, SHA256: hash, SizeBytes: int64(len(data))}, nil
}

func (r *MQRepository) AttachExecutionSnapshot(ctx context.Context, tx pgx.Tx, command contracts.Command, snapshot map[string]any) (contracts.Command, error) {
	var request GenerationRequest
	if err := json.Unmarshal(command.Payload, &request); err != nil {
		return contracts.Command{}, err
	}
	bundle := ExecutionBundle{
		SchemaVersion: "1.0", WorkspaceID: command.WorkspaceID, ProjectID: command.ProjectID,
		JobID: command.JobID, Snapshot: snapshot, Request: request,
		Inputs: []ContentVersion{}, ResolvedResources: map[string]any{},
	}
	if r.executionResourceResolver != nil {
		resources, err := r.executionResourceResolver(ctx, command.WorkspaceID, snapshot)
		if err != nil {
			return contracts.Command{}, err
		}
		bundle.ResolvedResources = resources
	}
	if request.SourceVersionID != "" {
		source, err := r.readContentVersion(ctx, tx, command.WorkspaceID, command.ProjectID, request.SourceVersionID)
		if err != nil {
			return contracts.Command{}, err
		}
		bundle.Source = &source
	}
	for _, versionID := range request.InputVersionIDs {
		input, err := r.readContentVersion(ctx, tx, command.WorkspaceID, command.ProjectID, versionID)
		if err != nil {
			return contracts.Command{}, err
		}
		bundle.Inputs = append(bundle.Inputs, input)
	}
	reference, err := r.writeExecutionSnapshot(ctx, bundle)
	if err != nil {
		return contracts.Command{}, err
	}
	command.ExecutionSnapshot = reference
	return command, nil
}

func (r *MQRepository) validateAssembleAudioRoles(ctx context.Context, tx pgx.Tx, workspace, project string, arguments map[string]any) error {
	object := arguments["assemble"].(map[string]any)
	check := func(versionID string, position int, expected string, allowUnmarked bool) error {
		version, err := r.readContentVersion(ctx, tx, workspace, project, versionID)
		if err != nil {
			return err
		}
		if version.Document.DocumentType != "audio" || position < 0 || position >= len(version.Document.Blocks) {
			return ErrMessageConflict
		}
		block := version.Document.Blocks[position]
		if block.Type != "audio" || block.Position != position {
			return ErrMessageConflict
		}
		role, exists := block.Metadata["audio_role"]
		if !exists && allowUnmarked {
			return nil
		}
		if role != expected {
			return ErrMessageConflict
		}
		return nil
	}
	for _, value := range object["shots"].([]any) {
		shot := value.(map[string]any)
		if versionID, ok := shot["audio_version_id"]; ok {
			position, _ := timelineInteger(shot, "audio_block_position", 0, timelineSafeInteger, 0)
			if err := check(versionID.(string), position, "narration", true); err != nil {
				return err
			}
		}
	}
	if versionID, ok := object["music_version_id"]; ok {
		position, _ := timelineInteger(object, "music_block_position", 0, timelineSafeInteger, 0)
		if err := check(versionID.(string), position, "music", false); err != nil {
			return err
		}
	}
	if values, ok := object["effects"].([]any); ok {
		for _, value := range values {
			effect := value.(map[string]any)
			position, _ := timelineInteger(effect, "block_position", 0, timelineSafeInteger, 0)
			if err := check(effect["version_id"].(string), position, "effect", false); err != nil {
				return err
			}
		}
	}
	return nil
}

func (r *MQRepository) Generate(ctx context.Context, workspace string, request GenerationRequest) (contracts.Command, error) {
	return r.generateWithSnapshot(ctx, workspace, request, nil, nil)
}

// Frozen overrides are private to durable production orchestration.
func (r *MQRepository) GenerateWithSnapshot(ctx context.Context, workspace string, request executionmodule.Request, frozen map[string]any, suppliedTx pgx.Tx) (contracts.Command, error) {
	return r.generateWithSnapshot(ctx, workspace, request, frozen, suppliedTx)
}

func (r *MQRepository) generateWithSnapshot(ctx context.Context, workspace string, request GenerationRequest, frozen map[string]any, suppliedTx pgx.Tx) (contracts.Command, error) {
	if !validGeneration(request) {
		return contracts.Command{}, ErrMessageConflict
	}
	// Freeze canonical references so equivalent UUID spellings replay the same request.
	request.ProjectID = uuid.MustParse(request.ProjectID).String()
	if request.DocumentID != "" {
		request.DocumentID = uuid.MustParse(request.DocumentID).String()
	}
	if request.SourceVersionID != "" {
		request.SourceVersionID = uuid.MustParse(request.SourceVersionID).String()
	}
	request.InputVersionIDs = append([]string(nil), request.InputVersionIDs...)
	for index, id := range request.InputVersionIDs {
		request.InputVersionIDs[index] = uuid.MustParse(id).String()
	}
	workflow := "content.generate@1"
	if request.SourceVersionID != "" {
		workflow = "content.adapt@1"
	}
	if request.DocumentType == "video" {
		workflow = "media.video@1"
	}
	if request.DocumentType == "image" {
		workflow = "media.image@1"
	}
	if request.DocumentType == "audio" {
		workflow = generatedAudioWorkflow(request.Arguments)
	}
	if request.DocumentType == "storyboard" || request.DocumentType == "timeline" {
		workflow = "media." + request.DocumentType + "@1"
	}
	if request.Operation == "compose" {
		workflow = "media.compose@1"
	}
	if request.Operation == "assemble" {
		workflow = "media.assemble@1"
	}
	if request.DocumentID == "" {
		if request.IdempotencyKey == "" {
			request.IdempotencyKey = uuid.NewString()
		}
		request.DocumentID = uuid.NewSHA1(uuid.NameSpaceURL, []byte("document:"+workspace+":"+request.IdempotencyKey)).String()
	}
	if request.IdempotencyKey == "" {
		request.IdempotencyKey = uuid.NewString()
	}
	commandID := uuid.NewSHA1(uuid.NameSpaceURL, []byte("command:"+workspace+":"+request.IdempotencyKey)).String()
	if len(request.Nodes) == 0 {
		request.Nodes = []string{"plan", "write", "review"}
		if request.DocumentType == "video" || request.DocumentType == "image" || request.DocumentType == "audio" {
			request.Nodes = []string{"plan", "generate", "review"}
		}
		if request.DocumentType == "storyboard" || request.DocumentType == "timeline" {
			request.Nodes = []string{"plan", "review"}
		}
		if request.Operation == "compose" {
			request.Nodes = []string{"validate", "render", "publish"}
		}
		if request.Operation == "assemble" {
			request.Nodes = []string{"validate", "assemble", "publish"}
		}
	}
	if request.RequiredMemoryVersions == nil {
		request.RequiredMemoryVersions = []string{}
	}
	snapshot := generationSnapshot(request, workflow)
	if frozen != nil {
		// Hash the frozen base without mutating it or chaining a prior child's hash.
		snapshot = make(map[string]any, len(frozen))
		for key, value := range frozen {
			if key != "snapshot_hash" {
				snapshot[key] = value
			}
		}
	}
	var replay *contracts.Command
	var command contracts.Command
	enqueue := func(tx pgx.Tx) error {
		if _, err := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", workspace+":"+request.IdempotencyKey); err != nil {
			return err
		}
		var previous []byte
		err := tx.QueryRow(ctx, "SELECT payload FROM command_outbox WHERE command_id=$1", commandID).Scan(&previous)
		if err == nil {
			var command contracts.Command
			if err = json.Unmarshal(previous, &command); err != nil {
				return err
			}
			var prior GenerationRequest
			if err = json.Unmarshal(command.Payload, &prior); err != nil {
				return err
			}
			a, b := prior, request
			a.Version = 0
			b.Version = 0
			x, _ := json.Marshal(a)
			y, _ := json.Marshal(b)
			if !sameJSON(x, y) {
				return ErrMessageConflict
			}
			replay = &command
			return nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		// Existing commands replay their original resource versions even after unpublication.
		if frozen == nil && r.snapshotFreezer != nil {
			snapshot, err = r.snapshotFreezer(ctx, workspace, snapshot)
			if err != nil {
				return err
			}
		}
		canonical, err := governance.CanonicalJSON(snapshot)
		if err != nil {
			return err
		}
		sum := sha256.Sum256(canonical)
		hash := hex.EncodeToString(sum[:])
		snapshot["snapshot_hash"] = hash
		encoded, err := json.Marshal(snapshot)
		if err != nil {
			return err
		}
		resolvedResources := map[string]any{}
		if r.executionResourceResolver != nil {
			resolvedResources, err = r.executionResourceResolver(ctx, workspace, snapshot)
			if err != nil {
				return err
			}
		}
		var project string
		if err := tx.QueryRow(ctx, "SELECT id FROM projects WHERE id=$1", request.ProjectID).Scan(&project); err != nil {
			return err
		}
		var source *ContentVersion
		inputs := make([]ContentVersion, 0, len(request.InputVersionIDs))
		if request.SourceVersionID != "" {
			value, err := r.readContentVersion(ctx, tx, workspace, project, request.SourceVersionID)
			if err != nil {
				return err
			}
			source = &value
			if request.Operation == "assemble" && !productionmodule.ValidAssembleSource(value.Document, request.Arguments, request.InputVersionIDs) {
				return ErrMessageConflict
			}
			if request.Operation == "assemble" {
				if err := r.validateAssembleAudioRoles(ctx, tx, workspace, project, request.Arguments); err != nil {
					return err
				}
			}
			if request.Operation == "compose" && !validComposeSource(value.Document, request.InputVersionIDs) {
				return ErrMessageConflict
			}
		}
		for _, id := range request.InputVersionIDs {
			input, err := r.readContentVersion(ctx, tx, workspace, project, id)
			if err != nil {
				return err
			}
			inputs = append(inputs, input)
		}
		if r.prepareContentDocument == nil {
			return errors.New("content document preparer unavailable")
		}
		request.Version, err = r.prepareContentDocument(ctx, tx, workspace, project, request.DocumentID, request.DocumentType, request.Title)
		if errors.Is(err, content.ErrConflict) {
			return ErrMessageConflict
		}
		if err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, "INSERT INTO capability_snapshots(workspace_id,content_hash,snapshot) VALUES($1,$2,$3) ON CONFLICT(workspace_id,content_hash) DO NOTHING", workspace, hash, encoded); err != nil {
			return err
		}
		jobID := uuid.NewSHA1(uuid.NameSpaceURL, []byte("job:"+commandID)).String()
		requestBody, err := json.Marshal(request)
		if err != nil {
			return err
		}
		bundle := ExecutionBundle{
			SchemaVersion:     "1.0",
			WorkspaceID:       workspace,
			ProjectID:         project,
			JobID:             jobID,
			Snapshot:          snapshot,
			Request:           request,
			Source:            source,
			Inputs:            inputs,
			ResolvedResources: resolvedResources,
		}
		executionSnapshot, err := r.writeExecutionSnapshot(ctx, bundle)
		if err != nil {
			return err
		}
		command = contracts.Command{SchemaVersion: "1.0", ID: commandID, Type: "workflow.run", WorkspaceID: workspace, ProjectID: request.ProjectID, JobID: jobID, WorkflowRunID: jobID, CapabilitySnapshotHash: hash, ExecutionSnapshot: executionSnapshot, TraceID: commandID, IdempotencyKey: request.IdempotencyKey, Payload: requestBody}
		return r.SubmitWithin(ctx, tx, command)
	}
	var err error
	if suppliedTx != nil {
		err = enqueue(suppliedTx)
	} else {
		err = r.within(ctx, workspace, enqueue)
	}
	if errors.Is(err, pgx.ErrNoRows) {
		err = ErrNotFound
	}
	if err != nil {
		return contracts.Command{}, err
	}
	if replay != nil {
		return *replay, nil
	}
	return command, nil
}

func (r *MQRepository) Execution(ctx context.Context, workspace, jobID string) (ExecutionBundle, error) {
	bundle := ExecutionBundle{
		SchemaVersion:     "1.0",
		WorkspaceID:       workspace,
		JobID:             jobID,
		Inputs:            []ContentVersion{},
		ResolvedResources: map[string]any{},
	}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		var data []byte
		if err := tx.QueryRow(ctx, "SELECT request FROM jobs WHERE id=$1", jobID).Scan(&data); err != nil {
			return err
		}
		var command contracts.Command
		if err := json.Unmarshal(data, &command); err != nil {
			return err
		}
		bundle.ProjectID = command.ProjectID
		if err := json.Unmarshal(command.Payload, &bundle.Request); err != nil {
			return err
		}
		if err := tx.QueryRow(ctx, "SELECT snapshot FROM capability_snapshots WHERE content_hash=$1", command.CapabilitySnapshotHash).Scan(&data); err != nil {
			return err
		}
		if err := json.Unmarshal(data, &bundle.Snapshot); err != nil {
			return err
		}
		if bundle.Request.SourceVersionID != "" {
			source, err := r.readContentVersion(ctx, tx, workspace, bundle.Request.ProjectID, bundle.Request.SourceVersionID)
			if err != nil {
				return err
			}
			bundle.Source = &source
		}
		for _, id := range bundle.Request.InputVersionIDs {
			input, err := r.readContentVersion(ctx, tx, workspace, bundle.Request.ProjectID, id)
			if err != nil {
				return err
			}
			bundle.Inputs = append(bundle.Inputs, input)
		}
		if r.contentVersionIDByJob == nil {
			return errors.New("content version identity reader unavailable")
		}
		version, err := r.contentVersionIDByJob(ctx, tx, workspace, jobID)
		if err == nil {
			bundle.Committed = map[string]any{"document_id": bundle.Request.DocumentID, "document_version_id": version, "source_version": bundle.Request.Version}
		} else if !errors.Is(err, pgx.ErrNoRows) {
			return err
		}
		return nil
	})
	if errors.Is(err, pgx.ErrNoRows) {
		err = ErrNotFound
	}
	return bundle, err
}

func (r *MQRepository) readContentVersion(ctx context.Context, tx pgx.Tx, workspace, project, version string) (ContentVersion, error) {
	if r.contentVersionReader == nil {
		return ContentVersion{}, errors.New("content version reader unavailable")
	}
	return r.contentVersionReader(ctx, tx, workspace, project, version)
}
func (r *MQRepository) ContentVersion(ctx context.Context, workspace, project, version string) (ContentVersion, error) {
	var result ContentVersion
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		var err error
		result, err = r.readContentVersion(ctx, tx, workspace, project, version)
		return err
	})
	if errors.Is(err, pgx.ErrNoRows) {
		err = ErrNotFound
	}
	return result, err
}

func (r *MQRepository) commitGenerated(ctx context.Context, tx pgx.Tx, event contracts.Event) error {
	var commit struct {
		DocumentID      string                           `json:"document_id"`
		VersionID       string                           `json:"document_version_id"`
		IRHash          string                           `json:"ir_hash"`
		SourceVersionID string                           `json:"source_version_id"`
		SourceIRHash    string                           `json:"source_ir_hash"`
		InputVersionIDs []string                         `json:"input_version_ids"`
		InputIRHashes   map[string]string                `json:"input_ir_hashes"`
		Workflow        string                           `json:"workflow"`
		Artifact        *objectstorage.ArtifactReference `json:"artifact"`
	}
	if err := json.Unmarshal(event.Payload, &commit); err != nil {
		return err
	}
	if commit.Artifact == nil {
		return nil
	}
	var status string
	if err := tx.QueryRow(ctx, "SELECT status FROM jobs WHERE id::text=$1", event.JobID).Scan(&status); err != nil {
		return err
	}
	if status == "cancelled" || status == "failed" {
		return nil
	}
	if status != "completed" {
		return ErrMessageConflict
	}
	if r.contentArtifacts == nil {
		return errors.New("content artifact reader is unavailable")
	}
	key, err := objectstorage.ObjectKey(event.WorkspaceID, event.ProjectID, "documents", commit.DocumentID, "content.json", commit.VersionID)
	if err != nil {
		return err
	}
	if key != commit.Artifact.ObjectKey {
		return ErrMessageConflict
	}
	body, err := r.contentArtifacts.Get(ctx, key, commit.Artifact.SHA256)
	if err != nil {
		return err
	}
	var document GeneratedDocument
	if err = json.Unmarshal(body, &document); err != nil {
		return err
	}
	if document.ProjectID != event.ProjectID || document.DocumentID != commit.DocumentID || document.ContentHash != commit.IRHash || document.SchemaVersion != "1.0" || len(document.Blocks) == 0 {
		return ErrMessageConflict
	}
	for index, block := range document.Blocks {
		if block.Text == "" || block.Type == "" || block.Position != index {
			return ErrMessageConflict
		}
	}
	var requestJSON []byte
	if err = tx.QueryRow(ctx, "SELECT request FROM jobs WHERE id::text=$1", event.JobID).Scan(&requestJSON); err != nil {
		return err
	}
	var command contracts.Command
	_ = json.Unmarshal(requestJSON, &command)
	var request GenerationRequest
	_ = json.Unmarshal(command.Payload, &request)
	if request.DocumentID != document.DocumentID || request.DocumentType != document.DocumentType || request.Version != document.Version {
		return ErrMessageConflict
	}
	inputs := make([]ContentVersion, 0, len(request.InputVersionIDs))
	for _, id := range request.InputVersionIDs {
		input, err := r.readContentVersion(ctx, tx, event.WorkspaceID, event.ProjectID, id)
		if err != nil {
			return err
		}
		inputs = append(inputs, input)
	}
	if !validCommittedInputs(request, commit.InputVersionIDs, commit.InputIRHashes, inputs) {
		return ErrMessageConflict
	}
	var sourceIRHash string
	if commit.SourceVersionID != "" {
		if request.SourceVersionID != commit.SourceVersionID {
			return ErrMessageConflict
		}
		source, err := r.readContentVersion(ctx, tx, event.WorkspaceID, event.ProjectID, commit.SourceVersionID)
		if err != nil {
			return err
		}
		if source.Document.ContentHash != commit.SourceIRHash {
			return ErrMessageConflict
		}
		sourceIRHash = commit.SourceIRHash
	}
	inputVersions := make([]content.VersionReference, 0, len(inputs))
	for _, input := range inputs {
		inputVersions = append(inputVersions, content.VersionReference{ID: input.VersionID, Hash: input.Document.ContentHash})
	}
	if r.commitContentVersion == nil {
		return errors.New("content version committer unavailable")
	}
	commitRequest := content.GeneratedVersionCommit{
		WorkspaceID: event.WorkspaceID, ProjectID: event.ProjectID, JobID: event.JobID,
		DocumentID: document.DocumentID, VersionID: commit.VersionID, DocumentType: document.DocumentType,
		SchemaVersion: document.SchemaVersion, ContentHash: document.ContentHash, Payload: body,
		SourceVersionID: commit.SourceVersionID, SourceIRHash: sourceIRHash, InputVersions: inputVersions,
		WorkflowRunID: event.WorkflowRunID, Workflow: commit.Workflow,
	}
	err = r.commitContentVersion(ctx, tx, commitRequest)
	if errors.Is(err, content.ErrConflict) {
		return ErrMessageConflict
	}
	return err
}

// Runtime may omit input provenance on older event envelopes; the frozen request
// and immutable database payload remain the authority in either case.
func validCommittedInputs(request GenerationRequest, ids []string, hashes map[string]string, inputs []ContentVersion) bool {
	if len(inputs) != len(request.InputVersionIDs) {
		return false
	}
	if ids != nil && !equalInputIDs(ids, request.InputVersionIDs) {
		return false
	}
	if hashes != nil && len(hashes) != len(inputs) {
		return false
	}
	for i, input := range inputs {
		if input.VersionID != request.InputVersionIDs[i] || hashes != nil && hashes[input.VersionID] != input.Document.ContentHash {
			return false
		}
	}
	return true
}

func equalInputIDs(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}

func generatedAudioWorkflow(arguments map[string]any) string {
	return executionmodule.GeneratedAudioWorkflow(arguments)
}

func audioWorkflowBinding(workflow string) (string, string) {
	return executionmodule.AudioWorkflowBinding(workflow)
}

func generationSnapshot(request GenerationRequest, workflow string) map[string]any {
	return executionmodule.DefaultCapabilitySnapshot(request, workflow)
}
