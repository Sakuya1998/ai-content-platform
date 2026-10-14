package postgres

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"unicode/utf8"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/sakuya/ai-content-platform/apps/control/internal/contracts"
	executionmodule "github.com/sakuya/ai-content-platform/apps/control/internal/modules/execution"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/production"
)

type ProductionBinding = production.Binding
type ProductionRequest = production.Request
type productionShot = production.Shot
type ProductionChild = production.Child
type ProductionRun = production.Run

func (r *Repository) readContentVersion(ctx context.Context, tx pgx.Tx, workspace, projectID, versionID string) (ContentVersion, error) {
	if r.contentVersion == nil {
		return ContentVersion{}, errors.New("content version reader unavailable")
	}
	return r.contentVersion(ctx, tx, workspace, projectID, versionID)
}

func (r *Repository) productionBindingVersion(ctx context.Context, tx pgx.Tx, workspace, project, id string, position int, visual bool) error {
	version, err := r.readContentVersion(ctx, tx, workspace, project, id)
	if err != nil {
		return err
	}
	if visual && version.Document.DocumentType != "image" && version.Document.DocumentType != "video" || !visual && version.Document.DocumentType != "audio" {
		return ErrMessageConflict
	}
	if position < 0 || position >= len(version.Document.Blocks) {
		return ErrMessageConflict
	}
	block := version.Document.Blocks[position]
	if block.Position != position || block.Type != version.Document.DocumentType {
		return ErrMessageConflict
	}
	asset, ok := block.Metadata["asset"].(map[string]any)
	if !ok || len(asset) == 0 {
		return ErrMessageConflict
	}
	return nil
}
func (r *Repository) readProduction(ctx context.Context, tx pgx.Tx, workspace, id string, lock bool) (ProductionRun, ContentVersion, map[string]map[string]any, error) {
	run := ProductionRun{Children: []ProductionChild{}}
	var source ContentVersion
	snapshots := map[string]map[string]any{}
	var req, src, snap []byte
	query := `SELECT id,project_id,status,request,source,snapshots,error_stage,error_code,coalesce(timeline_version_id::text,''),coalesce(final_version_id::text,''),created_at,updated_at FROM production_runs WHERE id=$1`
	if lock {
		query += " FOR UPDATE"
	}
	err := tx.QueryRow(ctx, query, id).Scan(&run.ID, &run.ProjectID, &run.Status, &req, &src, &snap, &run.ErrorStage, &run.ErrorCode, &run.TimelineVersionID, &run.FinalVersionID, &run.CreatedAt, &run.UpdatedAt)
	if err != nil {
		return run, source, snapshots, normalizeRepositoryError(err)
	}
	if err = json.Unmarshal(req, &run.Request); err != nil {
		return run, source, snapshots, err
	}
	if err = json.Unmarshal(src, &source); err != nil {
		return run, source, snapshots, err
	}
	if err = json.Unmarshal(snap, &snapshots); err != nil {
		return run, source, snapshots, err
	}
	rows, err := tx.Query(ctx, `SELECT c.slot,c.stage,c.shot_id,c.job_id,j.status,j.result FROM production_children c JOIN jobs j ON j.id=c.job_id WHERE c.run_id=$1 ORDER BY c.slot`, id)
	if err != nil {
		return run, source, snapshots, err
	}
	children := []ProductionChild{}
	for rows.Next() {
		var child ProductionChild
		if err = rows.Scan(&child.Slot, &child.Stage, &child.ShotID, &child.JobID, &child.Status, &child.Result); err != nil {
			rows.Close()
			return run, source, snapshots, err
		}
		children = append(children, child)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return run, source, snapshots, err
	}
	for _, child := range children {
		if r.versionIDBySourceJob != nil {
			versionID, lookupErr := r.versionIDBySourceJob(ctx, tx, workspace, child.JobID)
			if lookupErr != nil && !errors.Is(lookupErr, pgx.ErrNoRows) {
				return run, source, snapshots, lookupErr
			}
			child.VersionID = versionID
		}
		run.Children = append(run.Children, child)
	}
	return run, source, snapshots, nil
}
func (r *Repository) Production(ctx context.Context, workspace, id string) (ProductionRun, error) {
	var result ProductionRun
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		var err error
		result, _, _, err = r.readProduction(ctx, tx, workspace, id, false)
		return err
	})
	return result, err
}
func (r *Repository) Productions(ctx context.Context, workspace, project string) ([]ProductionRun, error) {
	result := []ProductionRun{}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `SELECT id FROM production_runs WHERE ($1='' OR project_id::text=$1) ORDER BY created_at DESC LIMIT 100`, project)
		if err != nil {
			return err
		}
		ids := []string{}
		for rows.Next() {
			var id string
			if err = rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			ids = append(ids, id)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for _, id := range ids {
			run, _, _, err := r.readProduction(ctx, tx, workspace, id, false)
			if err != nil {
				return err
			}
			result = append(result, run)
		}
		return nil
	})
	return result, err
}
func (r *Repository) CreateProduction(ctx context.Context, workspace string, request ProductionRequest) (ProductionRun, error) {
	normalizeProduction(&request)
	if !validProduction(request) {
		return ProductionRun{}, ErrMessageConflict
	}
	request.ProjectID = uuid.MustParse(request.ProjectID).String()
	request.StoryboardVersionID = uuid.MustParse(request.StoryboardVersionID).String()
	request.Bindings = append([]ProductionBinding(nil), request.Bindings...)
	for i := range request.Bindings {
		b := &request.Bindings[i]
		if b.VisualVersionID != "" {
			b.VisualVersionID = uuid.MustParse(b.VisualVersionID).String()
		}
		if b.AudioVersionID != "" {
			b.AudioVersionID = uuid.MustParse(b.AudioVersionID).String()
		}
	}
	id := uuid.NewSHA1(uuid.NameSpaceURL, []byte("production:"+workspace+":"+request.IdempotencyKey)).String()
	body, _ := json.Marshal(request)
	// Replays use the durable snapshots even if governance has since changed or disappeared.
	if previous, err := r.Production(ctx, workspace, id); err == nil {
		prior, _ := json.Marshal(previous.Request)
		if !sameJSON(prior, body) {
			return ProductionRun{}, ErrMessageConflict
		}
		return previous, nil
	} else if !errors.Is(err, ErrNotFound) {
		return ProductionRun{}, err
	}
	var source ContentVersion
	var shots []productionShot
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		var err error
		source, err = r.readContentVersion(ctx, tx, workspace, request.ProjectID, request.StoryboardVersionID)
		if err != nil {
			return err
		}
		shots, err = storyboardShots(source.Document)
		if err != nil {
			return err
		}
		lookup := map[string]bool{}
		for _, s := range shots {
			lookup[s.ShotID] = true
		}
		for _, b := range request.Bindings {
			if !lookup[b.ShotID] {
				return ErrMessageConflict
			}
			if b.VisualVersionID != "" {
				if e := r.productionBindingVersion(ctx, tx, workspace, request.ProjectID, b.VisualVersionID, b.VisualBlockPosition, true); e != nil {
					return e
				}
			}
			if b.AudioVersionID != "" {
				if e := r.productionBindingVersion(ctx, tx, workspace, request.ProjectID, b.AudioVersionID, b.AudioBlockPosition, false); e != nil {
					return e
				}
			}
		}
		return nil
	})
	if err != nil {
		return ProductionRun{}, normalizeRepositoryError(err)
	}
	bindings := map[string]ProductionBinding{}
	for _, b := range request.Bindings {
		bindings[b.ShotID] = b
	}
	needed := map[string]GenerationRequest{"assemble": {DocumentType: "timeline", Operation: "assemble"}, "compose": {DocumentType: "video", Operation: "compose"}}
	for _, s := range shots {
		b := bindings[s.ShotID]
		if b.VisualVersionID == "" {
			if len(productionVisualObjective(s)) > 16000 {
				return ProductionRun{}, ErrMessageConflict
			}
			needed[request.VisualMode] = GenerationRequest{DocumentType: request.VisualMode}
		}
		if b.AudioVersionID == "" && request.GenerateSpeech && strings.TrimSpace(s.Dialogue) != "" {
			needed["audio"] = GenerationRequest{DocumentType: "audio"}
		}
		if request.GenerateSoundEffects && s.DurationMS >= 500 {
			needed["effect"] = GenerationRequest{DocumentType: "audio", Arguments: map[string]any{"effect": request.SoundEffectParameters}}
		}
	}
	if request.GenerateMusic {
		needed["music"] = GenerationRequest{DocumentType: "audio", Arguments: map[string]any{"music": request.MusicParameters}}
	}
	snapshots := map[string]map[string]any{}
	for kind, brief := range needed {
		workflow := "media." + kind + "@1"
		if kind == "audio" {
			workflow = "media.speech@1"
		} else if kind == "effect" {
			workflow = "media.sound_effect@1"
		}
		snap := executionmodule.DefaultCapabilitySnapshot(brief, workflow)
		if r.snapshotFreezer != nil {
			snap, err = r.snapshotFreezer(ctx, workspace, snap)
			if err != nil {
				return ProductionRun{}, err
			}
		}
		snapshots[kind] = snap
	}
	var routeVersion string
	for _, snapshot := range snapshots {
		raw, _ := json.Marshal(snapshot["model_routes"])
		var routes map[string]string
		if e := json.Unmarshal(raw, &routes); e != nil {
			return ProductionRun{}, e
		}
		for _, version := range routes {
			if r.snapshotFreezer != nil {
				if routeVersion == "" {
					routeVersion = version
				}
				if version != routeVersion {
					return ProductionRun{}, ErrMessageConflict
				}
			}
		}
	}
	src, _ := json.Marshal(source)
	frozen, _ := json.Marshal(snapshots)
	err = r.within(ctx, workspace, func(tx pgx.Tx) error {
		if _, e := tx.Exec(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1,0))", workspace+":production:"+request.IdempotencyKey); e != nil {
			return e
		}
		var prior []byte
		e := tx.QueryRow(ctx, "SELECT request FROM production_runs WHERE id=$1", id).Scan(&prior)
		if e == nil {
			if !sameJSON(prior, body) {
				return ErrMessageConflict
			}
			return nil
		}
		if !errors.Is(e, pgx.ErrNoRows) {
			return e
		}
		if request.VisualMode == "video" && snapshots["video"] != nil {
			if e := r.validateProductionVideo(ctx, tx, workspace, request, shots, bindings, snapshots["video"]); e != nil {
				return e
			}
		}
		if r.snapshotFreezer != nil {
			if e := r.validateProductionMedia(ctx, tx, workspace, request, shots, bindings, snapshots); e != nil {
				return e
			}
		}
		_, e = tx.Exec(ctx, `INSERT INTO production_runs(id,workspace_id,project_id,idempotency_key,request,source,snapshots,status) VALUES($1,$2,$3,$4,$5,$6,$7,'queued')`, id, workspace, request.ProjectID, request.IdempotencyKey, body, src, frozen)
		return e
	})

	if err != nil {
		return ProductionRun{}, err
	}
	return r.Production(ctx, workspace, id)
}

// Read only immutable capability metadata: never decrypt or persist provider credentials.
func (r *Repository) productionFrozenProfile(ctx context.Context, tx pgx.Tx, workspace string, snapshot map[string]any, purpose string) (ModelProfile, string, error) {
	if r.frozenModelProfile == nil {
		return ModelProfile{}, "", errors.New("frozen model profile reader unavailable")
	}
	raw, _ := json.Marshal(snapshot["model_routes"])
	var routes map[string]string
	if e := json.Unmarshal(raw, &routes); e != nil {
		return ModelProfile{}, "", e
	}
	id := routes[purpose]
	var data []byte
	if e := tx.QueryRow(ctx, "SELECT routes FROM governance_model_routes WHERE workspace_id=$1 AND id::text=$2", workspace, id).Scan(&data); e != nil {
		return ModelProfile{}, "", e
	}
	var targets map[string]RouteTarget
	if e := json.Unmarshal(data, &targets); e != nil {
		return ModelProfile{}, "", e
	}
	target := targets[purpose]
	p, e := r.frozenModelProfile(ctx, tx, workspace, target.ProfileVersionID)
	return p, target.ModelName, e
}
func (r *Repository) productionVideoCapabilities(ctx context.Context, tx pgx.Tx, workspace string, snapshot map[string]any) (VideoCapabilities, error) {
	profile, name, e := r.productionFrozenProfile(ctx, tx, workspace, snapshot, "video_generation")
	if e != nil {
		return VideoCapabilities{}, e
	}
	caps, ok := videoModelCapabilities(profile.VideoModels, name)
	if !ok || !caps.TextToVideo {
		return caps, ErrMessageConflict
	}
	return caps, nil
}
func (r *Repository) validateProductionMedia(ctx context.Context, tx pgx.Tx, workspace string, request ProductionRequest, shots []productionShot, bindings map[string]ProductionBinding, snapshots map[string]map[string]any) error {
	if snapshot := snapshots["image"]; snapshot != nil {
		profile, name, e := r.productionFrozenProfile(ctx, tx, workspace, snapshot, "image_generation")
		if e != nil {
			return e
		}
		caps, ok := imageModelCapabilities(profile.ImageModels, name)
		if !ok || !caps.Generation || caps.MaxImages < 1 || len(caps.Formats) == 0 {
			return ErrMessageConflict
		}
		params := request.ImageParameters
		if size, exists := params["size"]; exists && !modelContains(caps.Sizes, size.(string)) {
			return ErrMessageConflict
		}
		format := caps.Formats[0]
		if value, exists := params["format"]; exists {
			format = value.(string)
		}
		if !modelContains(caps.Formats, format) {
			return ErrMessageConflict
		}
	}
	if snapshot := snapshots["audio"]; snapshot != nil {
		profile, name, e := r.productionFrozenProfile(ctx, tx, workspace, snapshot, "speech_synthesis")
		if e != nil {
			return e
		}
		caps, ok := speechModelCapabilities(profile.SpeechModels, name)
		if !ok || len(caps.Voices) == 0 || len(caps.Formats) == 0 {
			return ErrMessageConflict
		}
		params := request.AudioParameters
		voice := caps.Voices[0]
		if value, exists := params["voice"]; exists {
			voice = value.(string)
		}
		if !modelContains(caps.Voices, voice) {
			return ErrMessageConflict
		}
		format := caps.Formats[0]
		if value, exists := params["format"]; exists {
			format = value.(string)
		}
		if !modelContains(caps.Formats, format) {
			return ErrMessageConflict
		}
		if language, exists := params["language"]; exists && !modelContains(caps.Languages, language.(string)) {
			return ErrMessageConflict
		}
		speed := any(max(caps.MinSpeed, min(1.0, caps.MaxSpeed)))
		if value, exists := params["speed"]; exists {
			speed = value
		}
		if !boundedMediaReal(speed, caps.MinSpeed, caps.MaxSpeed) {
			return ErrMessageConflict
		}
		if _, exists := params["instructions"]; exists && !caps.Instructions {
			return ErrMessageConflict
		}
		for _, shot := range shots {
			if bindings[shot.ShotID].AudioVersionID == "" && strings.TrimSpace(shot.Dialogue) != "" && utf8.RuneCountInString(shot.Dialogue) > caps.MaxInputChars {
				return ErrMessageConflict
			}
		}
	}
	if snapshot := snapshots["music"]; snapshot != nil {
		profile, name, e := r.productionFrozenProfile(ctx, tx, workspace, snapshot, "music_generation")
		if e != nil {
			return e
		}
		caps, ok := musicModelCapabilities(profile.MusicModels, name)
		if !ok || len(caps.OutputFormats) == 0 {
			return ErrMessageConflict
		}
		format := caps.OutputFormats[0]
		if value, exists := request.MusicParameters["output_format"]; exists {
			format, ok = value.(string)
			if !ok {
				return ErrMessageConflict
			}
		}
		if !modelContains(caps.OutputFormats, format) {
			return ErrMessageConflict
		}
		if value, exists := request.MusicParameters["music_length_ms"]; exists && !boundedVideoInteger(value, float64(caps.MinDurationMS), float64(caps.MaxDurationMS)) {
			return ErrMessageConflict
		}
		if value, exists := request.MusicParameters["force_instrumental"]; exists {
			instrumental, ok := value.(bool)
			if !ok || instrumental && !caps.Instrumental {
				return ErrMessageConflict
			}
		}
	}
	if snapshot := snapshots["effect"]; snapshot != nil {
		profile, name, e := r.productionFrozenProfile(ctx, tx, workspace, snapshot, "sound_effect_generation")
		if e != nil {
			return e
		}
		caps, ok := soundEffectModelCapabilities(profile.SoundEffectModels, name)
		if !ok || len(caps.OutputFormats) == 0 {
			return ErrMessageConflict
		}
		format := caps.OutputFormats[0]
		if value, exists := request.SoundEffectParameters["output_format"]; exists {
			format, ok = value.(string)
			if !ok {
				return ErrMessageConflict
			}
		}
		if !modelContains(caps.OutputFormats, format) {
			return ErrMessageConflict
		}
		if value, exists := request.SoundEffectParameters["duration_seconds"]; exists && !boundedMediaReal(value, caps.MinDurationSeconds, caps.MaxDurationSeconds) {
			return ErrMessageConflict
		}
		if _, exists := request.SoundEffectParameters["prompt_influence"]; exists && !caps.PromptInfluence {
			return ErrMessageConflict
		}
		if value, exists := request.SoundEffectParameters["loop"]; exists {
			loop, ok := value.(bool)
			if !ok || loop && !caps.Loop {
				return ErrMessageConflict
			}
		}
	}
	return nil
}
func productionDuration(params map[string]any, caps VideoCapabilities, durationMS int) (int, error) {
	selected := 0
	if value, exists := params["duration"]; exists {
		object := map[string]any{"duration": value}
		duration, ok := timelineInteger(object, "duration", 1, 120, 0)
		if !ok || duration*1000 < durationMS {
			return 0, ErrMessageConflict
		}
		for _, allowed := range caps.Durations {
			if allowed == duration {
				return duration, nil
			}
		}
		return 0, ErrMessageConflict
	}
	for _, duration := range caps.Durations {
		if duration*1000 >= durationMS && (selected == 0 || duration < selected) {
			selected = duration
		}
	}
	if selected == 0 {
		return 0, ErrMessageConflict
	}
	return selected, nil
}
func (r *Repository) validateProductionVideo(ctx context.Context, tx pgx.Tx, workspace string, request ProductionRequest, shots []productionShot, bindings map[string]ProductionBinding, snapshot map[string]any) error {
	caps, err := r.productionVideoCapabilities(ctx, tx, workspace, snapshot)
	if err != nil {
		return err
	}
	for field, allowed := range map[string][]string{"ratio": caps.Ratios, "resolution": caps.Resolutions} {
		if value, exists := request.VideoParameters[field]; exists && !modelContains(allowed, value.(string)) {
			return ErrMessageConflict
		}
	}
	if request.VideoParameters["generate_audio"] == true && !caps.GenerateAudio {
		return ErrMessageConflict
	}
	for _, shot := range shots {
		if bindings[shot.ShotID].VisualVersionID == "" {
			if _, err := productionDuration(request.VideoParameters, caps, shot.DurationMS); err != nil {
				return err
			}
		}
	}
	return nil
}
func cloneParameters(params map[string]any) map[string]any {
	result := map[string]any{}
	for key, value := range params {
		result[key] = value
	}
	return result
}

func productionVisualObjective(shot productionShot) string {
	return "Generate exactly one visual for storyboard shot " + shot.ShotID + ": " + shot.Description + ". Use the source storyboard for consistency; do not combine other shots."
}

func productionChildTitle(title, runID, slot string) string {
	if len(title) > 120 {
		title = title[:120]
		for !utf8.ValidString(title) {
			title = title[:len(title)-1]
		}
	}
	return title + " [" + uuid.NewSHA1(uuid.NameSpaceURL, []byte(slot)).String() + "] " + runID
}
func (r *Repository) productionEnqueue(ctx context.Context, tx pgx.Tx, workspace string, run *ProductionRun, slot, stage, shot string, brief GenerationRequest, snapshot map[string]any) error {
	if r.commands == nil {
		return errors.New("production command port unavailable")
	}
	brief.IdempotencyKey = "production:" + run.ID + ":" + slot
	brief.ProjectID = run.ProjectID
	brief.Title = productionChildTitle(run.Request.Title, run.ID, slot)
	command, err := r.commands.GenerateWithSnapshot(ctx, workspace, brief, snapshot, tx)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, "INSERT INTO production_children(workspace_id,run_id,slot,stage,shot_id,job_id) VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(run_id,slot) DO NOTHING", workspace, run.ID, slot, stage, shot, command.JobID)
	return err
}
func (r *Repository) productionControl(ctx context.Context, tx pgx.Tx, workspace string, run ProductionRun, child ProductionChild, action string) error {
	if r.commands == nil {
		return errors.New("production command port unavailable")
	}
	var body []byte
	var sequence int64
	if err := tx.QueryRow(ctx, "SELECT request,last_event_sequence FROM jobs WHERE id=$1", child.JobID).Scan(&body, &sequence); err != nil {
		return err
	}
	var c contracts.Command
	if err := json.Unmarshal(body, &c); err != nil {
		return err
	}
	c.IdempotencyKey = "production:" + run.ID + ":" + action + ":" + child.JobID
	if action == "resume" {
		c.IdempotencyKey += fmt.Sprintf(":%d", sequence)
	}
	c.ID = uuid.NewSHA1(uuid.NameSpaceURL, []byte(workspace+":"+c.IdempotencyKey)).String()
	c.Type = "job." + action
	c.TraceID = c.ID
	c.Payload = json.RawMessage(`{}`)
	if action == "resume" {
		c.Payload = json.RawMessage(`{"action":"approve"}`)
	}
	return r.commands.SubmitWithin(ctx, tx, c)
}
func (r *Repository) cancelProductionChildren(ctx context.Context, tx pgx.Tx, workspace string, run ProductionRun) error {
	for _, child := range run.Children {
		if child.Status != "completed" && child.Status != "failed" && child.Status != "cancelled" && child.Status != "cancel_requested" {
			if e := r.productionControl(ctx, tx, workspace, run, child, "cancel"); e != nil {
				return e
			}
		}
	}
	return nil
}

func productionMusicObjective(title string, shots []productionShot) string {
	var builder strings.Builder
	builder.WriteString("Compose instrumental background music for the video titled: ")
	builder.WriteString(title)
	builder.WriteString(". Match the emotional arc and scene changes described here:")
	for _, shot := range shots {
		description := []rune(shot.Description)
		if len(description) > 96 {
			description = description[:96]
		}
		line := "\n- " + shot.ShotID + ": " + string(description)
		if builder.Len()+len(line) > 15000 {
			break
		}
		builder.WriteString(line)
	}
	return builder.String()
}

func productionEffectObjective(shot productionShot) string {
	description := []rune(shot.Description)
	if len(description) > 4000 {
		description = description[:4000]
	}
	return "Create a concise, nonverbal sound effect for storyboard shot " + shot.ShotID + ": " + string(description)
}

func (r *Repository) AdvanceProduction(ctx context.Context, workspace, id string) (ProductionRun, error) {
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		run, source, snapshots, e := r.readProduction(ctx, tx, workspace, id, true)
		if e != nil {
			return e
		}
		if run.Status == "cancelled" || run.Status == "failed" || run.Status == "completed" {
			return nil
		}
		for _, child := range run.Children {
			if child.Status == "failed" || child.Status == "cancelled" {
				if _, e = tx.Exec(ctx, "UPDATE production_runs SET status='failed',error_stage=$2,error_code='child_failed',updated_at=now() WHERE id=$1", id, child.Stage); e != nil {
					return e
				}
				return r.cancelProductionChildren(ctx, tx, workspace, run)
			}
		}
		for _, child := range run.Children {
			if child.Status == "waiting_review" {
				_, e = tx.Exec(ctx, "UPDATE production_runs SET status='waiting_review',error_stage=$2,error_code='child_waiting_review',updated_at=now() WHERE id=$1", id, child.Stage)
				return e
			}
		}
		// A review pause is durable until explicitly resumed, even if the projection later changes.
		if run.Status == "waiting_review" {
			return nil
		}
		shots, e := storyboardShots(source.Document)
		if e != nil {
			return e
		}
		bindings := map[string]ProductionBinding{}
		for _, b := range run.Request.Bindings {
			bindings[b.ShotID] = b
		}
		children := map[string]ProductionChild{}
		for _, c := range run.Children {
			children[c.Slot] = c
		}
		assembly := []any{}
		inputs := []string{}
		seen := map[string]bool{}
		addInput := func(id string) {
			if id != "" && !seen[id] {
				seen[id] = true
				inputs = append(inputs, id)
			}
		}
		ready := true
		musicVersion := ""
		if run.Request.GenerateMusic {
			child, exists := children["music"]
			if !exists {
				profile, name, e := r.productionFrozenProfile(ctx, tx, workspace, snapshots["music"], "music_generation")
				if e != nil {
					return e
				}
				caps, ok := musicModelCapabilities(profile.MusicModels, name)
				if !ok || len(caps.OutputFormats) == 0 {
					return ErrMessageConflict
				}
				params := cloneParameters(run.Request.MusicParameters)
				if _, exists := params["output_format"]; !exists {
					params["output_format"] = caps.OutputFormats[0]
				}
				if _, exists := params["music_length_ms"]; !exists {
					duration := 0
					for _, shot := range shots {
						duration += shot.DurationMS
					}
					params["music_length_ms"] = max(caps.MinDurationMS, min(duration, caps.MaxDurationMS))
				}
				brief := GenerationRequest{
					DocumentType: "audio", SourceVersionID: run.Request.StoryboardVersionID,
					Objective: productionMusicObjective(run.Request.Title, shots), Arguments: map[string]any{"music": params},
				}
				if e = r.productionEnqueue(ctx, tx, workspace, &run, "music", "assets", "", brief, snapshots["music"]); e != nil {
					return e
				}
				ready = false
			} else if child.Status == "completed" && child.VersionID != "" {
				musicVersion = child.VersionID
				addInput(musicVersion)
			} else {
				ready = false
			}
		}
		effects := []any{}
		for _, shot := range shots {
			b := bindings[shot.ShotID]
			b.ShotID = shot.ShotID
			if b.VisualVersionID == "" {
				slot := "visual:" + shot.ShotID
				child, exists := children[slot]
				if !exists {
					params := cloneParameters(run.Request.ImageParameters)
					if run.Request.VisualMode == "video" {
						params = cloneParameters(run.Request.VideoParameters)
						caps, e := r.productionVideoCapabilities(ctx, tx, workspace, snapshots["video"])
						if e != nil {
							return e
						}
						duration, e := productionDuration(params, caps, shot.DurationMS)
						if e != nil {
							return e
						}
						params["duration"] = duration
					}
					brief := GenerationRequest{DocumentType: run.Request.VisualMode, SourceVersionID: run.Request.StoryboardVersionID, Objective: productionVisualObjective(shot), Arguments: map[string]any{run.Request.VisualMode: params}}
					if e = r.productionEnqueue(ctx, tx, workspace, &run, slot, "assets", shot.ShotID, brief, snapshots[run.Request.VisualMode]); e != nil {
						return e
					}
					ready = false
				} else if child.Status == "completed" && child.VersionID != "" {
					b.VisualVersionID = child.VersionID
				} else {
					ready = false
				}
			}
			if b.AudioVersionID == "" && run.Request.GenerateSpeech && strings.TrimSpace(shot.Dialogue) != "" {
				slot := "audio:" + shot.ShotID
				child, exists := children[slot]
				if !exists {
					params := cloneParameters(run.Request.AudioParameters)
					params["text"] = shot.Dialogue
					brief := GenerationRequest{DocumentType: "audio", SourceVersionID: run.Request.StoryboardVersionID, Objective: "Speak the dialogue of storyboard shot " + shot.ShotID + " verbatim.", Arguments: map[string]any{"audio": params}}
					if e = r.productionEnqueue(ctx, tx, workspace, &run, slot, "assets", shot.ShotID, brief, snapshots["audio"]); e != nil {
						return e
					}
					ready = false
				} else if child.Status == "completed" && child.VersionID != "" {
					b.AudioVersionID = child.VersionID
				} else {
					ready = false
				}
			}
			effectVersion := ""
			if run.Request.GenerateSoundEffects && shot.DurationMS >= 500 {
				slot := "effect:" + shot.ShotID
				child, exists := children[slot]
				if !exists {
					profile, name, e := r.productionFrozenProfile(ctx, tx, workspace, snapshots["effect"], "sound_effect_generation")
					if e != nil {
						return e
					}
					caps, ok := soundEffectModelCapabilities(profile.SoundEffectModels, name)
					if !ok || len(caps.OutputFormats) == 0 {
						return ErrMessageConflict
					}
					params := cloneParameters(run.Request.SoundEffectParameters)
					if _, exists := params["output_format"]; !exists {
						params["output_format"] = caps.OutputFormats[0]
					}
					if _, exists := params["duration_seconds"]; !exists {
						duration := float64(shot.DurationMS) / 1000
						params["duration_seconds"] = max(caps.MinDurationSeconds, min(duration, caps.MaxDurationSeconds))
					}
					if _, exists := params["prompt_influence"]; !exists {
						params["prompt_influence"] = 0.3
					}
					brief := GenerationRequest{
						DocumentType: "audio", SourceVersionID: run.Request.StoryboardVersionID,
						Objective: productionEffectObjective(shot), Arguments: map[string]any{"effect": params},
					}
					if e = r.productionEnqueue(ctx, tx, workspace, &run, slot, "assets", shot.ShotID, brief, snapshots["effect"]); e != nil {
						return e
					}
					ready = false
				} else if child.Status == "completed" && child.VersionID != "" {
					effectVersion = child.VersionID
					addInput(effectVersion)
				} else {
					ready = false
				}
			}
			item := map[string]any{"shot_id": shot.ShotID, "visual_version_id": b.VisualVersionID, "visual_block_position": b.VisualBlockPosition}
			addInput(b.VisualVersionID)
			if b.AudioVersionID != "" {
				item["audio_version_id"] = b.AudioVersionID
				item["audio_block_position"] = b.AudioBlockPosition
				addInput(b.AudioVersionID)
			}
			assembly = append(assembly, item)
			if effectVersion != "" {
				effects = append(effects, map[string]any{"shot_id": shot.ShotID, "version_id": effectVersion, "block_position": 0})
			}
		}
		status := "assets"
		if ready {
			status = "assembling"
			assemble, exists := children["assemble"]
			if !exists {
				assembleArgs := map[string]any{"width": run.Request.Width, "height": run.Request.Height, "fps": run.Request.FPS, "subtitles": run.Request.Subtitles, "shots": assembly, "effects": effects}
				if musicVersion != "" {
					assembleArgs["music_version_id"] = musicVersion
					assembleArgs["music_block_position"] = 0
				}
				brief := GenerationRequest{DocumentType: "timeline", Operation: "assemble", SourceVersionID: run.Request.StoryboardVersionID, InputVersionIDs: inputs, Objective: "Assemble all storyboard shots in order with complete dialogue, music, and sound effects.", Arguments: map[string]any{"assemble": assembleArgs}}
				if e = r.productionEnqueue(ctx, tx, workspace, &run, "assemble", "assembling", "", brief, snapshots["assemble"]); e != nil {
					return e
				}
			} else if assemble.Status == "completed" && assemble.VersionID != "" {
				run.TimelineVersionID = assemble.VersionID
				status = "composing"
				compose, exists := children["compose"]
				if !exists {
					brief := GenerationRequest{DocumentType: "video", Operation: "compose", SourceVersionID: assemble.VersionID, InputVersionIDs: inputs, Objective: "Render the complete assembled storyboard into a finished video.", Arguments: map[string]any{"compose": map[string]any{"preset": "standard"}}}
					if e = r.productionEnqueue(ctx, tx, workspace, &run, "compose", "composing", "", brief, snapshots["compose"]); e != nil {
						return e
					}
				} else if compose.Status == "completed" && compose.VersionID != "" {
					status = "completed"
					run.FinalVersionID = compose.VersionID
				}
			}
		}
		_, e = tx.Exec(ctx, "UPDATE production_runs SET status=$2,timeline_version_id=NULLIF($3,'')::uuid,final_version_id=NULLIF($4,'')::uuid,error_stage='',error_code='',updated_at=now() WHERE id=$1", id, status, run.TimelineVersionID, run.FinalVersionID)
		return e
	})
	if errors.Is(err, ErrMessageConflict) {
		// A committed dependency that cannot satisfy the closed child contract
		// will not improve on periodic recovery. Persist its failing stage.
		err = r.within(ctx, workspace, func(tx pgx.Tx) error {
			run, _, _, e := r.readProduction(ctx, tx, workspace, id, true)
			if e != nil {
				return e
			}
			if run.Status == "completed" || run.Status == "cancelled" || run.Status == "failed" || run.Status == "waiting_review" {
				return nil
			}
			stage := run.Status
			if stage == "queued" {
				stage = "assets"
			}
			for _, child := range run.Children {
				if child.Slot == "assemble" && child.Status == "completed" && child.VersionID != "" {
					stage = "composing"
				}
			}
			if e = r.cancelProductionChildren(ctx, tx, workspace, run); e != nil {
				return e
			}
			_, e = tx.Exec(ctx, "UPDATE production_runs SET status='failed',error_stage=$2,error_code='invalid_dependency',updated_at=now() WHERE id=$1", id, stage)
			return e
		})
	}
	if err != nil {
		return ProductionRun{}, err
	}
	return r.Production(ctx, workspace, id)
}
func (r *Repository) CancelProduction(ctx context.Context, workspace, id string) (ProductionRun, error) {
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		run, _, _, e := r.readProduction(ctx, tx, workspace, id, true)
		if e != nil {
			return e
		}
		if run.Status == "cancelled" {
			return nil
		}
		if run.Status == "completed" {
			return ErrMessageConflict
		}
		if e = r.cancelProductionChildren(ctx, tx, workspace, run); e != nil {
			return e
		}
		_, e = tx.Exec(ctx, "UPDATE production_runs SET status='cancelled',updated_at=now() WHERE id=$1", id)
		return e
	})
	if err != nil {
		return ProductionRun{}, err
	}
	return r.Production(ctx, workspace, id)
}
func (r *Repository) ResumeProduction(ctx context.Context, workspace, id, childID, action string) (ProductionRun, error) {
	if action != "" && action != "approve" {
		return ProductionRun{}, ErrMessageConflict
	}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		run, _, _, e := r.readProduction(ctx, tx, workspace, id, true)
		if e != nil {
			return e
		}
		if run.Status != "waiting_review" {
			return ErrMessageConflict
		}
		found := childID == ""
		waiting := false
		for _, child := range run.Children {
			if childID != "" && child.JobID != childID {
				continue
			}
			if child.JobID == childID {
				found = true
			}
			if child.Status == "waiting_review" {
				waiting = true
				if action != "approve" {
					return ErrMessageConflict
				}
				if e = r.productionControl(ctx, tx, workspace, run, child, "resume"); e != nil {
					return e
				}
			}
		}
		if !found || childID != "" && !waiting {
			return ErrMessageConflict
		}
		_, e = tx.Exec(ctx, "UPDATE production_runs SET status='assets',error_stage='',error_code='',updated_at=now() WHERE id=$1", id)
		return e
	})
	if err != nil {
		return ProductionRun{}, err
	}
	return r.AdvanceProduction(ctx, workspace, id)
}
func (r *Repository) RecoverProductions(ctx context.Context) error {
	scopes, e := r.workspaces(ctx)
	if e != nil {
		return e
	}
	for _, workspace := range scopes {
		ids := []string{}
		e = r.within(ctx, workspace, func(tx pgx.Tx) error {
			rows, e := tx.Query(ctx, "SELECT id FROM production_runs WHERE status IN ('queued','assets','assembling','composing') ORDER BY updated_at LIMIT 100")
			if e != nil {
				return e
			}
			defer rows.Close()
			for rows.Next() {
				var id string
				if e = rows.Scan(&id); e != nil {
					return e
				}
				ids = append(ids, id)
			}
			return rows.Err()
		})
		if e != nil {
			return e
		}
		for _, id := range ids {
			if _, e = r.AdvanceProduction(ctx, workspace, id); e != nil && ctx.Err() != nil {
				return ctx.Err()
			}
		}
	}
	return nil
}
