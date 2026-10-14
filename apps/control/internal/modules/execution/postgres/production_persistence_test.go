package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"unicode/utf8"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/sakuya/novel-agent/apps/control/internal/contracts"
	contentpostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/content/postgres"
	governancepostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/governance/postgres"
	productionmodule "github.com/sakuya/novel-agent/apps/control/internal/modules/production"
	productionpostgres "github.com/sakuya/novel-agent/apps/control/internal/modules/production/postgres"
	projectmodule "github.com/sakuya/novel-agent/apps/control/internal/modules/project"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/config"
	"github.com/sakuya/novel-agent/apps/control/internal/platform/objectstorage"
)

type ProductionBinding = productionmodule.Binding
type ProductionRequest = productionmodule.Request
type ProductionChild = productionmodule.Child
type ProductionRun = productionmodule.Run

var validProduction = productionmodule.Valid
var validAssembleArguments = productionmodule.ValidAssembleArguments
var validAssembleSource = productionmodule.ValidAssembleSource

type productionFixture struct {
	repo                           *productionFixtureRepository
	workspace, project, storyboard string
	request                        ProductionRequest
}

type productionFixtureRepository struct {
	*MQRepository
	*productionpostgres.Repository
}

func (r *productionFixtureRepository) WithSnapshotFreezer(freezer SnapshotFreezer) *productionFixtureRepository {
	r.MQRepository.WithSnapshotFreezer(freezer)
	r.Repository.WithSnapshotFreezer(freezer)
	return r
}

type failingSecondSnapshotWriter struct {
	executionSnapshotWriterStub
	calls int
}

func (w *failingSecondSnapshotWriter) Put(ctx context.Context, key string, body []byte, contentType string) (objectstorage.ArtifactReference, error) {
	w.calls++
	if w.calls == 2 {
		return objectstorage.ArtifactReference{}, errors.New("injected storage outage")
	}
	return w.executionSnapshotWriterStub.Put(ctx, key, body, contentType)
}

func newProductionFixture(t *testing.T, count int, speech bool) productionFixture {
	t.Helper()
	pool := identityTestPool(t)
	ctx := context.Background()
	scope := uuid.NewString()
	if _, err := pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Production persistence test')", scope); err != nil {
		t.Fatal(err)
	}
	project, err := createProjectFixture(pool, projectmodule.Project{WorkspaceID: scope, Name: "Production", ProjectType: "generic"})
	if err != nil {
		t.Fatal(err)
	}
	commands := newTestMQRepository(pool, config.MQTopics{Tasks: "tasks", Control: "control"}).WithExecutionSnapshotStore(&executionSnapshotWriterStub{}).WithFrozenModelProfileReader(governancepostgres.FrozenModelProfile)
	productionRepository := productionpostgres.NewRepository(pool, commands, productionpostgres.RepositorySettings{FrozenModelProfile: governancepostgres.FrozenModelProfile, ContentVersion: contentpostgres.ReadVersionWithin, VersionIDBySourceJob: contentpostgres.VersionIDBySourceJob})
	repo := &productionFixtureRepository{MQRepository: commands, Repository: productionRepository}
	t.Cleanup(func() {
		// The integration database may subsequently host a live recovery loop;
		// fixture commands must never be relayed into that loop after the test.
		_ = repo.within(context.Background(), scope, func(tx pgx.Tx) error {
			if _, e := tx.Exec(context.Background(), "UPDATE production_runs SET status='cancelled' WHERE status NOT IN ('completed','failed','cancelled')"); e != nil {
				return e
			}
			if _, e := tx.Exec(context.Background(), "UPDATE jobs SET status='cancelled' WHERE status NOT IN ('completed','failed','cancelled')"); e != nil {
				return e
			}
			_, e := tx.Exec(context.Background(), "UPDATE command_outbox SET published_at=now() WHERE published_at IS NULL")
			return e
		})
	})
	shots := []any{}
	for i := 0; i < count; i++ {
		dialogue := ""
		if speech {
			dialogue = fmt.Sprintf("Literal dialogue %d", i)
		}
		shots = append(shots, map[string]any{"shot_id": fmt.Sprintf("shot%d", i), "description": fmt.Sprintf("Visual shot %d", i), "dialogue": dialogue, "duration_ms": 1000, "source_block_ids": []any{}})
	}
	source := GeneratedDocument{DocumentID: uuid.NewString(), ProjectID: project.ID, DocumentType: "storyboard", Version: 1, SchemaVersion: "1.0", ContentHash: uuid.NewString(), Blocks: []GeneratedBlock{{Type: "storyboard_shot", Text: "Storyboard", Position: 0}}, Metadata: map[string]any{"storyboard": map[string]any{"schema_version": "storyboard@1", "shots": shots}}}
	version := uuid.NewString()
	putProductionVersion(t, commands, scope, version, "", source)
	return productionFixture{repo: repo, workspace: scope, project: project.ID, storyboard: version, request: ProductionRequest{ProjectID: project.ID, StoryboardVersionID: version, Title: "Film", IdempotencyKey: uuid.NewString(), VisualMode: "image", Width: 1280, Height: 720, FPS: 25, Subtitles: true, GenerateSpeech: speech}}
}
func putProductionVersion(t *testing.T, repo *MQRepository, workspace, version, job string, document GeneratedDocument) {
	t.Helper()
	ctx := context.Background()
	body, err := json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	err = repo.within(ctx, workspace, func(tx pgx.Tx) error {
		if _, e := tx.Exec(ctx, "INSERT INTO documents(id,workspace_id,project_id,document_type,title) VALUES($1,$2,$3,$4,$5) ON CONFLICT(id) DO NOTHING", document.DocumentID, workspace, document.ProjectID, document.DocumentType, document.DocumentID); e != nil {
			return e
		}
		_, e := tx.Exec(ctx, `INSERT INTO document_versions(id,workspace_id,document_id,version,ir_schema_version,content_hash,payload,status,source_job_id) VALUES($1,$2,$3,$4,'1.0',$5,$6,'approved',NULLIF($7,'')::uuid)`, version, workspace, document.DocumentID, document.Version, document.ContentHash, body, job)
		return e
	})
	if err != nil {
		t.Fatal(err)
	}
}
func productionSetJob(t *testing.T, f productionFixture, job, status string, result string) {
	t.Helper()
	if result == "" {
		result = "{}"
	}
	err := f.repo.within(context.Background(), f.workspace, func(tx pgx.Tx) error {
		_, e := tx.Exec(context.Background(), "UPDATE jobs SET status=$2,result=$3,last_event_sequence=last_event_sequence+1 WHERE id=$1", job, status, result)
		return e
	})
	if err != nil {
		t.Fatal(err)
	}
}
func productionRun(t *testing.T, f productionFixture) ProductionRun {
	t.Helper()
	run, err := f.repo.CreateProduction(context.Background(), f.workspace, f.request)
	if err != nil {
		t.Fatal(err)
	}
	return run
}
func productionAdvance(t *testing.T, f productionFixture, id string) ProductionRun {
	t.Helper()
	run, err := f.repo.AdvanceProduction(context.Background(), f.workspace, id)
	if err != nil {
		t.Fatal(err)
	}
	return run
}
func productionChild(t *testing.T, run ProductionRun, slot string) ProductionChild {
	t.Helper()
	for _, c := range run.Children {
		if c.Slot == slot {
			return c
		}
	}
	t.Fatalf("child %s missing from %#v", slot, run.Children)
	return ProductionChild{}
}
func productionPublish(t *testing.T, f productionFixture, child ProductionChild) string {
	t.Helper()
	bundle, err := f.repo.Execution(context.Background(), f.workspace, child.JobID)
	if err != nil {
		t.Fatal(err)
	}
	req := bundle.Request
	metadata := map[string]any{}
	if req.Operation == "assemble" {
		args := req.Arguments["assemble"].(map[string]any)
		items := args["shots"].([]any)
		assets, shots := []any{}, []any{}
		for i, item := range items {
			shot := item.(map[string]any)
			asset := fmt.Sprintf("asset%d", i)
			assets = append(assets, map[string]any{"asset_id": asset, "document_version_id": shot["visual_version_id"], "block_position": shot["visual_block_position"]})
			shots = append(shots, map[string]any{"shot_id": shot["shot_id"], "asset_id": asset, "start_ms": i * 1000, "duration_ms": 1000})
		}
		metadata["timeline"] = map[string]any{"schema_version": "timeline@1", "width": 1280, "height": 720, "fps": 25, "duration_ms": len(items) * 1000, "assets": assets, "shots": shots, "audio_tracks": []any{}, "subtitles": []any{}}
	}
	blockMetadata := map[string]any{}
	if _, ok := req.Arguments["music"]; ok {
		blockMetadata["audio_role"] = "music"
	}
	if _, ok := req.Arguments["effect"]; ok {
		blockMetadata["audio_role"] = "effect"
	}
	d := GeneratedDocument{DocumentID: req.DocumentID, ProjectID: f.project, DocumentType: req.DocumentType, Version: req.Version, SchemaVersion: "1.0", ContentHash: uuid.NewString(), Metadata: metadata, Blocks: []GeneratedBlock{{Type: req.DocumentType, Text: "Published", Position: 0, Metadata: blockMetadata}}}
	version := uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, version, child.JobID, d)
	return version
}
func productionCounts(t *testing.T, f productionFixture, id string) (int, int) {
	t.Helper()
	var children, commands int
	err := f.repo.within(context.Background(), f.workspace, func(tx pgx.Tx) error {
		if e := tx.QueryRow(context.Background(), "SELECT count(*) FROM production_children WHERE run_id=$1", id).Scan(&children); e != nil {
			return e
		}
		return tx.QueryRow(context.Background(), `SELECT count(*) FROM command_outbox o JOIN production_children c ON c.job_id=o.job_id WHERE c.run_id=$1 AND o.payload->>'command_type'='workflow.run'`, id).Scan(&commands)
	})
	if err != nil {
		t.Fatal(err)
	}
	return children, commands
}

func TestProductionPersistenceReplayAndFrozenSnapshots(t *testing.T) {
	f := newProductionFixture(t, 1, true)
	ctx := context.Background()
	firstRoute := productionFixtureRoutes(t, f)
	route := firstRoute
	freezes := 0
	f.repo.WithSnapshotFreezer(func(_ context.Context, _ string, base map[string]any) (map[string]any, error) {
		freezes++
		routes := map[string]string{}
		for key := range base["model_routes"].(map[string]string) {
			routes[key] = route
		}
		base["model_routes"] = routes
		return base, nil
	})
	run := productionRun(t, f)
	if freezes != 4 {
		t.Fatalf("expected image/speech/assemble/compose frozen at creation; got %d", freezes)
	}
	route = uuid.NewString()
	replay := productionRun(t, f)
	if replay.ID != run.ID || freezes != 4 {
		t.Fatal("replay refroze or replaced run")
	}
	changed := f.request
	changed.Title = "Different"
	if _, err := f.repo.CreateProduction(ctx, f.workspace, changed); !errors.Is(err, ErrMessageConflict) {
		t.Fatalf("idempotency conflict: %v", err)
	}
	run = productionAdvance(t, f, run.ID)
	if len(run.Children) != 2 {
		t.Fatal("visual and speech children missing")
	}
	for _, child := range run.Children {
		bundle, err := f.repo.Execution(ctx, f.workspace, child.JobID)
		if err != nil {
			t.Fatal(err)
		}
		for _, value := range bundle.Snapshot["model_routes"].(map[string]any) {
			if value != firstRoute {
				t.Fatal("child adopted changed default route")
			}
		}
		if bundle.Request.SourceVersionID != f.storyboard {
			t.Fatal("child lost frozen storyboard")
		}
		if child.Slot == "audio:shot0" && bundle.Request.Arguments["audio"].(map[string]any)["text"] != "Literal dialogue 0" {
			t.Fatal("dialogue was rewritten")
		}
	}
	if _, err := f.repo.Production(ctx, uuid.NewString(), run.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("run visible outside tenant: %v", err)
	}
}

func TestAutomaticProductionFreezesMusicAndSoundEffectsIntoAssembly(t *testing.T) {
	f := newProductionFixture(t, 2, false)
	f.request.GenerateSpeech = false
	f.request.GenerateMusic = true
	f.request.GenerateSoundEffects = true
	route := productionFixtureRoutes(t, f)
	f.repo.WithSnapshotFreezer(productionFixtureFreezer(route))
	run := productionAdvance(t, f, productionRun(t, f).ID)
	if len(run.Children) != 5 {
		t.Fatalf("expected two visuals, one score and two effects; got %d children", len(run.Children))
	}
	for _, child := range run.Children {
		if child.Slot != "music" && !strings.HasPrefix(child.Slot, "effect:") {
			continue
		}
		bundle, err := f.repo.Execution(context.Background(), f.workspace, child.JobID)
		if err != nil {
			t.Fatal(err)
		}
		if bundle.Request.DocumentType != "audio" || bundle.Request.SourceVersionID != f.storyboard {
			t.Fatalf("%s did not preserve audio document and storyboard source", child.Slot)
		}
		if routes := bundle.Snapshot["model_routes"].(map[string]any); routes["music_generation"] != route && routes["sound_effect_generation"] != route {
			t.Fatalf("%s route was not frozen: %#v", child.Slot, routes)
		}
		productionPublish(t, f, child)
		productionSetJob(t, f, child.JobID, "completed", `{"approved":true}`)
	}
	for _, child := range run.Children {
		if child.Slot == "music" || strings.HasPrefix(child.Slot, "effect:") {
			continue
		}
		productionPublish(t, f, child)
		productionSetJob(t, f, child.JobID, "completed", `{"approved":true}`)
	}
	run = productionAdvance(t, f, run.ID)
	assemble := productionChild(t, run, "assemble")
	bundle, err := f.repo.Execution(context.Background(), f.workspace, assemble.JobID)
	if err != nil {
		t.Fatal(err)
	}
	arguments := bundle.Request.Arguments["assemble"].(map[string]any)
	if arguments["music_version_id"] == nil || len(arguments["effects"].([]any)) != 2 {
		t.Fatalf("music and shot effects were not bound into assembly: %#v", arguments)
	}
}

func TestProductionConcurrentAdvanceAtomicReplayAndCancel(t *testing.T) {
	f := newProductionFixture(t, 2, true)
	run := productionRun(t, f)
	ctx := context.Background()
	var wg sync.WaitGroup
	errs := make(chan error, 12)
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); _, e := f.repo.AdvanceProduction(ctx, f.workspace, run.ID); errs <- e }()
	}
	wg.Wait()
	close(errs)
	for e := range errs {
		if e != nil {
			t.Fatal(e)
		}
	}
	children, commands := productionCounts(t, f, run.ID)
	if children != 4 || commands != 4 {
		t.Fatalf("duplicate or non-atomic children/outbox: %d/%d", children, commands)
	}
	current, err := f.repo.Production(ctx, f.workspace, run.ID)
	if err != nil {
		t.Fatal(err)
	}
	hashes := map[string]any{}
	for _, child := range current.Children {
		bundle, e := f.repo.Execution(ctx, f.workspace, child.JobID)
		if e != nil {
			t.Fatal(e)
		}
		if prior, ok := hashes[bundle.Request.DocumentType]; ok && prior != bundle.Snapshot["snapshot_hash"] {
			t.Fatal("identical frozen child snapshots hashed differently")
		}
		hashes[bundle.Request.DocumentType] = bundle.Snapshot["snapshot_hash"]
	}
	// Recover an outbox-created child whose dependency row was lost by an old non-atomic writer.
	err = f.repo.within(ctx, f.workspace, func(tx pgx.Tx) error {
		_, e := tx.Exec(ctx, "DELETE FROM production_children WHERE run_id=$1 AND slot='visual:shot0'", run.ID)
		return e
	})
	if err != nil {
		t.Fatal(err)
	}
	run = productionAdvance(t, f, run.ID)
	if n, c := productionCounts(t, f, run.ID); n != 4 || c != 4 {
		t.Fatalf("replay created duplicate job: %d/%d", n, c)
	}
	for _, child := range run.Children {
		productionSetJob(t, f, child.JobID, "running", `{"details":{"remote_work_terminal":false,"provider_cancel_supported":false}}`)
	}
	run, err = f.repo.CancelProduction(ctx, f.workspace, run.ID)
	if err != nil {
		t.Fatal(err)
	}
	if run.Status != "cancelled" {
		t.Fatal("parent cancellation not durable")
	}
	for _, child := range run.Children {
		if child.Status != "cancel_requested" {
			t.Fatalf("remote job claimed terminal: %s", child.Status)
		}
		var value map[string]any
		if json.Unmarshal(child.Result, &value) != nil || value["details"].(map[string]any)["remote_work_terminal"] != false {
			t.Fatal("remote cancellation provenance missing")
		}
	}
	for i := 0; i < 3; i++ {
		run = productionAdvance(t, f, run.ID)
	}
	if n, c := productionCounts(t, f, run.ID); n != 4 || c != 4 {
		t.Fatal("cancelled run scheduled future work")
	}
	var cancels int
	err = f.repo.within(ctx, f.workspace, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, `SELECT count(*) FROM command_outbox o JOIN production_children c ON c.job_id=o.job_id WHERE c.run_id=$1 AND o.payload->>'command_type'='job.cancel'`, run.ID).Scan(&cancels)
	})
	if err != nil || cancels != 4 {
		t.Fatalf("cancel command count=%d error=%v", cancels, err)
	}
	if _, err = f.repo.CancelProduction(ctx, f.workspace, run.ID); err != nil {
		t.Fatal("cancel replay failed", err)
	}
}

func TestProductionWaitsForPublishedVersionsThroughFinal(t *testing.T) {
	f := newProductionFixture(t, 1, false)
	run := productionAdvance(t, f, productionRun(t, f).ID)
	visual := productionChild(t, run, "visual:shot0")
	productionSetJob(t, f, visual.JobID, "completed", "")
	run = productionAdvance(t, f, run.ID)
	if len(run.Children) != 1 || run.Status != "assets" {
		t.Fatal("assembly advanced before asset version landed")
	}
	productionPublish(t, f, visual)
	run = productionAdvance(t, f, run.ID)
	assemble := productionChild(t, run, "assemble")
	if run.Status != "assembling" {
		t.Fatal(run.Status)
	}
	bundle, err := f.repo.Execution(context.Background(), f.workspace, assemble.JobID)
	if err != nil {
		t.Fatal(err)
	}
	if bundle.Request.Operation != "assemble" || bundle.Snapshot["workflow"] != "media.assemble@1" {
		t.Fatal("incorrect controlled assembly job")
	}
	productionSetJob(t, f, assemble.JobID, "completed", "")
	run = productionAdvance(t, f, run.ID)
	if len(run.Children) != 2 || run.TimelineVersionID != "" {
		t.Fatal("compose advanced before timeline version landed")
	}
	timeline := productionPublish(t, f, assemble)
	run = productionAdvance(t, f, run.ID)
	compose := productionChild(t, run, "compose")
	if run.Status != "composing" || run.TimelineVersionID != timeline {
		t.Fatal("timeline not linked")
	}
	productionSetJob(t, f, compose.JobID, "completed", "")
	run = productionAdvance(t, f, run.ID)
	if run.Status != "composing" || run.FinalVersionID != "" {
		t.Fatal("parent completed without actual content version")
	}
	final := productionPublish(t, f, compose)
	run = productionAdvance(t, f, run.ID)
	if run.Status != "completed" || run.FinalVersionID != final {
		t.Fatal("published final did not complete parent")
	}
	children, commands := productionCounts(t, f, run.ID)
	if children != 3 || commands != 3 {
		t.Fatalf("chain jobs mismatch: %d/%d", children, commands)
	}
}

func TestProductionReviewResumeAndSiblingFailure(t *testing.T) {
	f := newProductionFixture(t, 2, false)
	run := productionAdvance(t, f, productionRun(t, f).ID)
	review := productionChild(t, run, "visual:shot0")
	sibling := productionChild(t, run, "visual:shot1")
	productionSetJob(t, f, review.JobID, "waiting_review", `{"details":{"remote_work_terminal":false}}`)
	run = productionAdvance(t, f, run.ID)
	if run.Status != "waiting_review" || run.ErrorStage != "assets" {
		t.Fatal("review not propagated")
	}
	run = productionAdvance(t, f, run.ID)
	if n, _ := productionCounts(t, f, run.ID); n != 2 {
		t.Fatal("review silently retried paid generation")
	}
	if _, err := f.repo.ResumeProduction(context.Background(), f.workspace, run.ID, review.JobID, ""); !errors.Is(err, ErrMessageConflict) {
		t.Fatal("approval required for paused child")
	}
	run, err := f.repo.ResumeProduction(context.Background(), f.workspace, run.ID, review.JobID, "approve")
	if err != nil {
		t.Fatal(err)
	}
	if productionChild(t, run, review.Slot).Status != "queued" || run.Status != "assets" {
		t.Fatal("explicit approval did not resume child")
	}
	var commandJSON []byte
	err = f.repo.within(context.Background(), f.workspace, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(), "SELECT payload FROM command_outbox WHERE job_id=$1 AND payload->>'command_type'='job.resume'", review.JobID).Scan(&commandJSON)
	})
	if err != nil {
		t.Fatal(err)
	}
	var c contracts.Command
	if json.Unmarshal(commandJSON, &c) != nil || !sameJSON(c.Payload, []byte(`{"action":"approve"}`)) {
		t.Fatalf("approval command invalid: %s", commandJSON)
	}
	// A later review episode requires a fresh stable control command, never a resubmitted paid task.
	productionSetJob(t, f, review.JobID, "waiting_review", "")
	run = productionAdvance(t, f, run.ID)
	run, err = f.repo.ResumeProduction(context.Background(), f.workspace, run.ID, review.JobID, "approve")
	if err != nil || run.Status != "assets" || productionChild(t, run, review.Slot).Status != "queued" {
		t.Fatalf("second review episode could not resume: status=%s error=%v", run.Status, err)
	}
	if _, err = f.repo.ResumeProduction(context.Background(), f.workspace, run.ID, review.JobID, "approve"); !errors.Is(err, ErrMessageConflict) {
		t.Fatal("same-cycle replay should not enqueue another approval")
	}
	var approvals int
	err = f.repo.within(context.Background(), f.workspace, func(tx pgx.Tx) error {
		return tx.QueryRow(context.Background(), "SELECT count(*) FROM command_outbox WHERE job_id=$1 AND payload->>'command_type'='job.resume'", review.JobID).Scan(&approvals)
	})
	if err != nil || approvals != 2 {
		t.Fatalf("review cycle approval count=%d error=%v", approvals, err)
	}
	productionSetJob(t, f, review.JobID, "failed", "")
	productionSetJob(t, f, sibling.JobID, "running", "")
	run = productionAdvance(t, f, run.ID)
	if run.Status != "failed" || run.ErrorStage != "assets" || productionChild(t, run, sibling.Slot).Status != "cancel_requested" {
		t.Fatal("failure did not stop active sibling")
	}
	for i := 0; i < 3; i++ {
		run = productionAdvance(t, f, run.ID)
	}
	if n, _ := productionCounts(t, f, run.ID); n != 2 {
		t.Fatal("failed parent scheduled future work")
	}
}

func TestProductionRejectsForeignBindingsShotLimitsAndAtomicFailure(t *testing.T) {
	f := newProductionFixture(t, 1, false)
	ctx := context.Background()
	other := newProductionFixture(t, 1, false)
	foreign := f.request
	foreign.Bindings = []ProductionBinding{{ShotID: "shot0", VisualVersionID: other.storyboard}}
	if _, err := f.repo.CreateProduction(ctx, f.workspace, foreign); err == nil {
		t.Fatal("foreign project version bound")
	}
	tooMany := newProductionFixture(t, 33, false)
	if _, err := tooMany.repo.CreateProduction(ctx, tooMany.workspace, tooMany.request); !errors.Is(err, ErrMessageConflict) {
		t.Fatalf("33 shots accepted: %v", err)
	}
	duration := f.request
	source, err := f.repo.ContentVersion(ctx, f.workspace, f.project, f.storyboard)
	if err != nil {
		t.Fatal(err)
	}
	manifest := source.Document.Metadata["storyboard"].(map[string]any)
	manifest["shots"].([]any)[0].(map[string]any)["duration_ms"] = 600001
	source.Document.DocumentID = uuid.NewString()
	source.Document.ContentHash = uuid.NewString()
	duration.StoryboardVersionID = uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, duration.StoryboardVersionID, "", source.Document)
	if _, err := f.repo.CreateProduction(ctx, f.workspace, duration); !errors.Is(err, ErrMessageConflict) {
		t.Fatalf("overlong production accepted: %v", err)
	}
	// Crash/failure during the second child must roll back the first child's document, job, outbox and dependency.
	f.request.GenerateSpeech = true
	source, err = f.repo.ContentVersion(ctx, f.workspace, f.project, f.storyboard)
	if err != nil {
		t.Fatal(err)
	}
	source.Document.DocumentID = uuid.NewString()
	source.Document.ContentHash = uuid.NewString()
	source.Document.Metadata["storyboard"].(map[string]any)["shots"].([]any)[0].(map[string]any)["dialogue"] = "Dialogue"
	f.request.StoryboardVersionID = uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, f.request.StoryboardVersionID, "", source.Document)
	run := productionRun(t, f)
	f.repo.WithExecutionSnapshotStore(&failingSecondSnapshotWriter{})
	if _, err := f.repo.AdvanceProduction(ctx, f.workspace, run.ID); err == nil {
		t.Fatal("injected enqueue failure did not fail")
	}
	if n, c := productionCounts(t, f, run.ID); n != 0 || c != 0 {
		t.Fatalf("partial child enqueue survived rollback: %d/%d", n, c)
	}
}

func TestProductionUnicodeChildBriefAndPreflightLimits(t *testing.T) {
	f := newProductionFixture(t, 1, false)
	ctx := context.Background()
	f.request.Title = strings.Repeat("片", 120)
	source, err := f.repo.ContentVersion(ctx, f.workspace, f.project, f.storyboard)
	if err != nil {
		t.Fatal(err)
	}
	source.Document.DocumentID = uuid.NewString()
	source.Document.ContentHash = uuid.NewString()
	source.Document.Metadata["storyboard"].(map[string]any)["shots"].([]any)[0].(map[string]any)["shot_id"] = strings.Repeat("s", 128)
	f.request.StoryboardVersionID = uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, f.request.StoryboardVersionID, "", source.Document)
	run := productionAdvance(t, f, productionRun(t, f).ID)
	if len(run.Children) != 1 {
		t.Fatal("Unicode title failed to enqueue child")
	}
	bundle, err := f.repo.Execution(ctx, f.workspace, run.Children[0].JobID)
	if err != nil {
		t.Fatal(err)
	}
	if len(bundle.Request.Title) > 200 || !utf8.ValidString(bundle.Request.Title) {
		t.Fatal("generated title violates brief byte limit")
	}
	source.Document.DocumentID = uuid.NewString()
	source.Document.ContentHash = uuid.NewString()
	source.Document.Metadata["storyboard"].(map[string]any)["shots"].([]any)[0].(map[string]any)["description"] = strings.Repeat("景", 8000)
	f.request.StoryboardVersionID = uuid.NewString()
	f.request.IdempotencyKey = uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, f.request.StoryboardVersionID, "", source.Document)
	if _, err = f.repo.CreateProduction(ctx, f.workspace, f.request); !errors.Is(err, ErrMessageConflict) {
		t.Fatalf("unusable long generated brief persisted: %v", err)
	}
}

func productionFixtureRoutes(t *testing.T, f productionFixture, alternateDefaults ...bool) string {
	t.Helper()
	ctx := context.Background()
	profile, version, route, user := uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString()
	images, _ := json.Marshal([]ImageModel{{ModelName: "image-a", Capabilities: ImageCapabilities{Generation: true, Sizes: []string{"1024x1024"}, Formats: []string{"png"}, MaxImages: 1}}})
	speech, _ := json.Marshal([]SpeechModel{{ModelName: "speech-a", Capabilities: SpeechCapabilities{Voices: []string{"alloy"}, Formats: []string{"mp3"}, Languages: []string{"en", "zh"}, MinSpeed: 0.5, MaxSpeed: 2, MaxInputChars: 64}}})
	music, _ := json.Marshal([]MusicModel{{ModelName: "music_v2_5", Capabilities: MusicCapabilities{OutputFormats: []string{"mp3_44100_128"}, MinDurationMS: 3000, MaxDurationMS: 600000, Instrumental: true}}})
	effects, _ := json.Marshal([]SoundEffectModel{{ModelName: "eleven_text_to_sound_v2", Capabilities: SoundEffectCapabilities{OutputFormats: []string{"mp3_22050_32"}, MinDurationSeconds: 0.5, MaxDurationSeconds: 30, PromptInfluence: true, Loop: true}}})
	videos, _ := json.Marshal([]VideoModel{{ModelName: "video-a", Capabilities: VideoCapabilities{TextToVideo: true, Durations: []int{2, 5}, Ratios: []string{"16:9"}, Resolutions: []string{"720p"}}}})
	if len(alternateDefaults) > 0 && alternateDefaults[0] {
		images, _ = json.Marshal([]ImageModel{{ModelName: "image-a", Capabilities: ImageCapabilities{Generation: true, Sizes: []string{"1024x1024"}, Formats: []string{"jpeg"}, MaxImages: 1}}})
		speech, _ = json.Marshal([]SpeechModel{{ModelName: "speech-a", Capabilities: SpeechCapabilities{Voices: []string{"alloy"}, Formats: []string{"wav"}, Languages: []string{"en", "zh"}, MinSpeed: 1.5, MaxSpeed: 2, MaxInputChars: 64}}})
	}
	targets, _ := json.Marshal(map[string]RouteTarget{"image_generation": {ProfileID: profile, ProfileVersionID: version, ModelName: "image-a"}, "speech_synthesis": {ProfileID: profile, ProfileVersionID: version, ModelName: "speech-a"}, "music_generation": {ProfileID: profile, ProfileVersionID: version, ModelName: "music_v2_5"}, "sound_effect_generation": {ProfileID: profile, ProfileVersionID: version, ModelName: "eleven_text_to_sound_v2"}, "video_generation": {ProfileID: profile, ProfileVersionID: version, ModelName: "video-a"}})
	err := f.repo.within(ctx, f.workspace, func(tx pgx.Tx) error {
		if _, e := tx.Exec(ctx, "INSERT INTO users(id,workspace_id,email,role) VALUES($1,$2,'production-test@example.test','owner')", user, f.workspace); e != nil {
			return e
		}
		if _, e := tx.Exec(ctx, "INSERT INTO governance_model_profiles(id,workspace_id,current_version) VALUES($1,$2,1)", profile, f.workspace); e != nil {
			return e
		}
		if _, e := tx.Exec(ctx, `INSERT INTO governance_model_profile_versions(id,workspace_id,profile_id,version,name,provider,image_models,speech_models,music_models,sound_effect_models,video_models) VALUES($1,$2,$3,1,'Production fixture','elevenlabs',$4,$5,$6,$7,$8)`, version, f.workspace, profile, images, speech, music, effects, videos); e != nil {
			return e
		}
		_, e := tx.Exec(ctx, "INSERT INTO governance_model_routes(id,workspace_id,version,routes,created_by) VALUES($1,$2,1,$3,$4)", route, f.workspace, targets, user)
		return e
	})
	if err != nil {
		t.Fatal(err)
	}
	return route
}
func productionFixtureFreezer(route string) SnapshotFreezer {
	return func(_ context.Context, _ string, base map[string]any) (map[string]any, error) {
		routes := map[string]string{}
		for key := range base["model_routes"].(map[string]string) {
			routes[key] = route
		}
		base["model_routes"] = routes
		return base, nil
	}
}

func TestProductionRejectsInvalidFrozenMediaParametersBeforeChildren(t *testing.T) {
	f := newProductionFixture(t, 1, true)
	ctx := context.Background()
	route := productionFixtureRoutes(t, f)
	f.repo.WithSnapshotFreezer(productionFixtureFreezer(route))
	for _, test := range []struct {
		name                string
		image, video, audio map[string]any
	}{
		{name: "editing storyboard", image: map[string]any{"operation": "edit"}},
		{name: "unused image quantity", image: map[string]any{"n": 2}},
		{name: "wrong image size", image: map[string]any{"size": "512x512"}},
		{name: "wrong image format", image: map[string]any{"format": "webp"}},
		{name: "unknown voice", audio: map[string]any{"voice": "unavailable"}},
		{name: "unsupported speech format", audio: map[string]any{"format": "opus"}},
		{name: "unsupported language", audio: map[string]any{"language": "fr"}},
		{name: "unsupported speed", audio: map[string]any{"speed": 4}},
		{name: "unsupported instructions", audio: map[string]any{"instructions": "Calm"}},
	} {
		t.Run(test.name, func(t *testing.T) {
			request := f.request
			request.IdempotencyKey = uuid.NewString()
			request.ImageParameters = test.image
			request.VideoParameters = test.video
			request.AudioParameters = test.audio
			if _, err := f.repo.CreateProduction(ctx, f.workspace, request); !errors.Is(err, ErrMessageConflict) {
				t.Fatalf("invalid frozen parameters persisted: %v", err)
			}
		})
	}
	source, err := f.repo.ContentVersion(ctx, f.workspace, f.project, f.storyboard)
	if err != nil {
		t.Fatal(err)
	}
	source.Document.DocumentID = uuid.NewString()
	source.Document.ContentHash = uuid.NewString()
	source.Document.Metadata["storyboard"].(map[string]any)["shots"].([]any)[0].(map[string]any)["dialogue"] = strings.Repeat("文", 65)
	f.request.StoryboardVersionID = uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, f.request.StoryboardVersionID, "", source.Document)
	if _, err := f.repo.CreateProduction(ctx, f.workspace, f.request); !errors.Is(err, ErrMessageConflict) {
		t.Fatal("selected speech input limit ignored", err)
	}
	runs, err := f.repo.Productions(ctx, f.workspace, f.project)
	if err != nil || len(runs) != 0 {
		t.Fatalf("failed preflight persisted parent: count=%d error=%v", len(runs), err)
	}
	var count int
	err = f.repo.within(ctx, f.workspace, func(tx pgx.Tx) error {
		return tx.QueryRow(ctx, "SELECT count(*) FROM command_outbox WHERE workspace_id=$1 AND payload->>'command_type'='workflow.run'", f.workspace).Scan(&count)
	})
	if err != nil || count != 0 {
		t.Fatalf("preflight scheduled paid job: count=%d error=%v", count, err)
	}
}

func TestProductionBindingIndexReuseAndSameProject(t *testing.T) {
	f := newProductionFixture(t, 1, false)
	ctx := context.Background()
	asset := GeneratedDocument{DocumentID: uuid.NewString(), ProjectID: f.project, DocumentType: "image", Version: 1, SchemaVersion: "1.0", ContentHash: uuid.NewString(), Blocks: []GeneratedBlock{{Type: "image", Text: "Asset", Position: 0, Metadata: map[string]any{"asset": map[string]any{"object_key": "image.png"}}}}}
	version := uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, version, "", asset)
	f.request.Bindings = []ProductionBinding{{ShotID: "shot0", VisualVersionID: strings.ToUpper(version)}}
	original := f.request.Bindings[0].VisualVersionID
	run := productionRun(t, f)
	if f.request.Bindings[0].VisualVersionID != original {
		t.Fatal("create mutated caller binding slice")
	}
	run = productionAdvance(t, f, run.ID)
	if len(run.Children) != 1 || run.Children[0].Slot != "assemble" {
		t.Fatal("bound image incorrectly generated a new visual")
	}
	other, err := createProjectFixture(f.repo.pool, projectmodule.Project{WorkspaceID: f.workspace, Name: "Other project", ProjectType: "generic"})
	if err != nil {
		t.Fatal(err)
	}
	asset.DocumentID = uuid.NewString()
	asset.ProjectID = other.ID
	asset.ContentHash = uuid.NewString()
	foreign := uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, foreign, "", asset)
	request := f.request
	request.IdempotencyKey = uuid.NewString()
	request.Bindings = []ProductionBinding{{ShotID: "shot0", VisualVersionID: foreign}}
	if _, err = f.repo.CreateProduction(ctx, f.workspace, request); !errors.Is(err, ErrNotFound) {
		t.Fatalf("same tenant foreign project binding accepted: %v", err)
	}
	asset.DocumentID = uuid.NewString()
	asset.ProjectID = f.project
	asset.ContentHash = uuid.NewString()
	asset.Blocks[0].Position = 3
	malformed := uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, malformed, "", asset)
	request.IdempotencyKey = uuid.NewString()
	request.Bindings = []ProductionBinding{{ShotID: "shot0", VisualVersionID: malformed, VisualBlockPosition: 3}}
	if _, err = f.repo.CreateProduction(ctx, f.workspace, request); !errors.Is(err, ErrMessageConflict) {
		t.Fatalf("binding used metadata position instead of actual array index: %v", err)
	}
	asset.DocumentID = uuid.NewString()
	asset.ContentHash = uuid.NewString()
	asset.Blocks[0].Position = 0
	asset.Blocks[0].Metadata = nil
	missingAsset := uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, missingAsset, "", asset)
	request.IdempotencyKey = uuid.NewString()
	request.Bindings = []ProductionBinding{{ShotID: "shot0", VisualVersionID: missingAsset}}
	if _, err = f.repo.CreateProduction(ctx, f.workspace, request); !errors.Is(err, ErrMessageConflict) {
		t.Fatalf("non-media block bound: %v", err)
	}
}

func TestProductionFrozenVideoDurationPreflight(t *testing.T) {
	f := newProductionFixture(t, 1, false)
	ctx := context.Background()
	route := productionFixtureRoutes(t, f)
	f.repo.WithSnapshotFreezer(productionFixtureFreezer(route))
	f.request.VisualMode = "video"
	for _, params := range []map[string]any{{"duration": 1}, {"ratio": "1:1"}, {"resolution": "1080p"}, {"generate_audio": true}, {"image_url": "https://example.test/image.png"}} {
		request := f.request
		request.IdempotencyKey = uuid.NewString()
		request.VideoParameters = params
		if _, err := f.repo.CreateProduction(ctx, f.workspace, request); !errors.Is(err, ErrMessageConflict) {
			t.Fatalf("unsupported frozen video parameters accepted: %v %v", params, err)
		}
	}
	run := productionAdvance(t, f, productionRun(t, f).ID)
	bundle, err := f.repo.Execution(ctx, f.workspace, run.Children[0].JobID)
	if err != nil {
		t.Fatal(err)
	}
	if bundle.Request.Arguments["video"].(map[string]any)["duration"] != float64(2) {
		t.Fatalf("incorrect supported duration %v", bundle.Request.Arguments)
	}
	source, err := f.repo.ContentVersion(ctx, f.workspace, f.project, f.storyboard)
	if err != nil {
		t.Fatal(err)
	}
	source.Document.DocumentID = uuid.NewString()
	source.Document.ContentHash = uuid.NewString()
	source.Document.Metadata["storyboard"].(map[string]any)["shots"].([]any)[0].(map[string]any)["duration_ms"] = 5001
	f.request.StoryboardVersionID = uuid.NewString()
	f.request.IdempotencyKey = uuid.NewString()
	putProductionVersion(t, f.repo.MQRepository, f.workspace, f.request.StoryboardVersionID, "", source.Document)
	if _, err = f.repo.CreateProduction(ctx, f.workspace, f.request); !errors.Is(err, ErrMessageConflict) {
		t.Fatalf("too short available video generation accepted: %v", err)
	}
}

func TestProductionInvalidCommittedDependencyFailsDurably(t *testing.T) {
	f := newProductionFixture(t, 1, false)
	ctx := context.Background()
	run := productionAdvance(t, f, productionRun(t, f).ID)
	visual := productionChild(t, run, "visual:shot0")
	productionSetJob(t, f, visual.JobID, "completed", "")
	productionPublish(t, f, visual)
	run = productionAdvance(t, f, run.ID)
	assemble := productionChild(t, run, "assemble")
	bundle, err := f.repo.Execution(ctx, f.workspace, assemble.JobID)
	if err != nil {
		t.Fatal(err)
	}
	invalid := GeneratedDocument{DocumentID: bundle.Request.DocumentID, ProjectID: f.project, DocumentType: "timeline", Version: 1, SchemaVersion: "1.0", ContentHash: uuid.NewString(), Metadata: map[string]any{}, Blocks: []GeneratedBlock{{Type: "timeline", Text: "Invalid timeline", Position: 0}}}
	putProductionVersion(t, f.repo.MQRepository, f.workspace, uuid.NewString(), assemble.JobID, invalid)
	productionSetJob(t, f, assemble.JobID, "completed", "")
	run, err = f.repo.AdvanceProduction(ctx, f.workspace, run.ID)
	if err != nil || run.Status != "failed" || run.ErrorStage != "composing" || run.ErrorCode != "invalid_dependency" {
		t.Fatalf("invalid committed dependency stuck in recovery: status=%s stage=%s error=%v", run.Status, run.ErrorStage, err)
	}
	if n, _ := productionCounts(t, f, run.ID); n != 2 {
		t.Fatal("invalid dependency launched renderer")
	}
}

func TestProductionOmittedParametersUseFrozenCapabilitiesDefaults(t *testing.T) {
	f := newProductionFixture(t, 1, true)
	route := productionFixtureRoutes(t, f, true)
	f.repo.WithSnapshotFreezer(productionFixtureFreezer(route))
	run := productionAdvance(t, f, productionRun(t, f).ID)
	if len(run.Children) != 2 {
		t.Fatal("available model defaults should allow image and speech")
	}
	for _, child := range run.Children {
		bundle, e := f.repo.Execution(context.Background(), f.workspace, child.JobID)
		if e != nil {
			t.Fatal(e)
		}
		for _, field := range []string{"format", "speed"} {
			if _, specified := bundle.Request.Arguments[bundle.Request.DocumentType].(map[string]any)[field]; specified {
				t.Fatal("omitted defaults should remain capability-owned")
			}
		}
	}
}
