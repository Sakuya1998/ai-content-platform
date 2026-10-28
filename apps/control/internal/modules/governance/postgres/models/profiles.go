package models

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/database"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
)

func (repo *Repository) ExecuteModelCatalogOp(
	ctx context.Context,
	p Principal,
	parts []string,
	action governance.Action,
	body json.RawMessage,
) (statusCode int, result any, err error) {
	tx, err := database.BeginWorkspaceTx(ctx, repo.pool, p.WorkspaceID)
	if err != nil {
		return 0, nil, err
	}
	defer tx.Rollback(ctx)

	if (len(parts) == 0 || len(parts) == 1 && parts[0] == "profiles") && action == governance.Read {
		return repo.listProfiles(ctx, tx, p.WorkspaceID)
	}
	if len(parts) == 1 && parts[0] == "routes" && (action == governance.Replace || action == governance.Create) {
		return repo.saveRoutes(ctx, tx, p, body)
	}
	if len(parts) == 0 || parts[0] != "profiles" {
		return 404, nil, governance.ErrNotFound
	}
	if len(parts) > 3 {
		return 404, nil, governance.ErrNotFound
	}

	id := ""
	current := governance.ModelProfile{}
	if len(parts) >= 2 && parts[1] != "" {
		id = parts[1]
		err = tx.QueryRow(ctx, "SELECT id::text FROM governance_model_profiles WHERE workspace_id=$1 AND id::text=$2 AND NOT deleted FOR UPDATE", p.WorkspaceID, id).Scan(&id)
		if err == nil {
			current, err = ScanModelProfile(tx.QueryRow(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND p.id=$2 AND v.version=p.current_version", p.WorkspaceID, id))
		}
		if err != nil {
			return 0, nil, err
		}
	}
	if len(parts) == 3 && parts[2] == "test" && action == governance.Create {
		return repo.testModel(ctx, current, body)
	}
	if len(parts) > 2 {
		return 404, nil, governance.ErrNotFound
	}
	if action == governance.Read && id != "" {
		return 200, current, nil
	}
	if action == governance.Delete && id != "" {
		return repo.deleteProfile(ctx, tx, p, id)
	}
	if !(action == governance.Create && id == "" || (action == governance.Replace || action == governance.Update) && id != "") {
		return 405, nil, governance.ErrMethodNotAllowed
	}
	return repo.writeProfile(ctx, tx, p, id, current, body)
}

func (repo *Repository) listProfiles(ctx context.Context, tx pgx.Tx, workspace string) (int, any, error) {
	rows, err := tx.Query(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND NOT p.deleted AND v.version=p.current_version ORDER BY v.name", workspace)
	if err != nil {
		return 0, nil, err
	}
	profiles := []governance.ModelProfile{}
	for rows.Next() {
		profile, scanErr := ScanModelProfile(rows)
		if scanErr != nil {
			rows.Close()
			return 0, nil, scanErr
		}
		profiles = append(profiles, profile)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return 0, nil, err
	}
	var id string
	var version int
	var routes json.RawMessage
	err = tx.QueryRow(ctx, "SELECT id::text,version,routes FROM governance_model_routes WHERE workspace_id=$1 ORDER BY version DESC LIMIT 1", workspace).Scan(&id, &version, &routes)
	if errors.Is(err, pgx.ErrNoRows) {
		routes = json.RawMessage(`{}`)
		err = nil
	}
	if err != nil {
		return 0, nil, err
	}
	return 200, map[string]any{"profiles": profiles, "routes": routes, "route_version_id": id, "route_version": version}, nil
}

func (repo *Repository) saveRoutes(ctx context.Context, tx pgx.Tx, p Principal, body json.RawMessage) (int, any, error) {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	var targets map[string]RouteTarget
	if err := decoder.Decode(&targets); err != nil {
		return 400, nil, errors.New("invalid request body")
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return 400, nil, errors.New("request body must contain one JSON object")
	}
	for _, kind := range []string{"creative", "analysis", "embedding"} {
		if _, ok := targets[kind]; !ok {
			return 422, nil, errors.New("creative, analysis, embedding routes required")
		}
	}
	for kind, target := range targets {
		if !governance.ValidRouteKind(kind) {
			return 422, nil, errors.New("unknown route kind")
		}
		frozen, err := repo.FreezeTarget(ctx, tx, p.WorkspaceID, kind, target)
		if err != nil {
			return 422, nil, errors.New("invalid model route")
		}
		targets[kind] = frozen
	}
	if _, err := tx.Exec(ctx, "SELECT id FROM workspaces WHERE id=$1 FOR UPDATE", p.WorkspaceID); err != nil {
		return 0, nil, err
	}
	var version int
	if err := tx.QueryRow(ctx, "SELECT coalesce(max(version),0)+1 FROM governance_model_routes WHERE workspace_id=$1", p.WorkspaceID).Scan(&version); err != nil {
		return 0, nil, err
	}
	id := uuid.NewString()
	data, err := json.Marshal(targets)
	if err != nil {
		return 0, nil, err
	}
	if _, err = tx.Exec(ctx, "INSERT INTO governance_model_routes(id,workspace_id,version,routes,created_by) VALUES($1,$2,$3,$4,$5)", id, p.WorkspaceID, version, data, p.UserID); err != nil {
		return 0, nil, err
	}
	if err = repo.writeAudit(ctx, tx, p, "governance.model_routes.updated", "model_routes", id); err != nil {
		return 0, nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, nil, err
	}
	return 200, map[string]any{"route_version_id": id, "version": version, "routes": targets}, nil
}

func (repo *Repository) deleteProfile(ctx context.Context, tx pgx.Tx, p Principal, id string) (int, any, error) {
	var data []byte
	err := tx.QueryRow(ctx, "SELECT routes FROM governance_model_routes WHERE workspace_id=$1 ORDER BY version DESC LIMIT 1", p.WorkspaceID).Scan(&data)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return 0, nil, err
	}
	var targets map[string]RouteTarget
	_ = json.Unmarshal(data, &targets)
	for _, target := range targets {
		if governance.RouteUsesProfile(target, id) {
			return 409, nil, errors.New("profile is used by current routes")
		}
	}
	if _, err = tx.Exec(ctx, "UPDATE governance_model_profiles SET deleted=true WHERE workspace_id=$1 AND id=$2", p.WorkspaceID, id); err != nil {
		return 0, nil, err
	}
	if err = repo.writeAudit(ctx, tx, p, "governance.model_profile.deleted", "model_profile", id); err != nil {
		return 0, nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return 0, nil, err
	}
	return 200, map[string]any{"deleted": true, "profile_id": id}, nil
}

func (repo *Repository) writeProfile(ctx context.Context, tx pgx.Tx, p Principal, id string, current governance.ModelProfile, body json.RawMessage) (int, any, error) {
	var input governance.ModelProfileWrite
	if err := decodeModelProfileWrite(body, &input); err != nil {
		return 400, nil, err
	}
	if input.VideoModels == nil {
		input.VideoModels = current.VideoModels
	}
	if input.ImageModels == nil {
		input.ImageModels = current.ImageModels
	}
	if input.SpeechModels == nil {
		input.SpeechModels = current.SpeechModels
	}
	if input.MusicModels == nil {
		input.MusicModels = current.MusicModels
	}
	if input.SoundEffectModels == nil {
		input.SoundEffectModels = current.SoundEffectModels
	}
	if err := governance.ValidateModelProfile(input); err != nil {
		return 422, nil, err
	}
	if input.MaxTokens <= 0 {
		input.MaxTokens = 4096
	}
	if input.ChatModels == nil {
		input.ChatModels = []string{}
	}
	if input.EmbeddingModels == nil {
		input.EmbeddingModels = []string{}
	}
	if input.VideoModels == nil {
		input.VideoModels = []governance.VideoModel{}
	}
	if input.ImageModels == nil {
		input.ImageModels = []governance.ImageModel{}
	}
	if input.SpeechModels == nil {
		input.SpeechModels = []governance.SpeechModel{}
	}
	if input.MusicModels == nil {
		input.MusicModels = []governance.MusicModel{}
	}
	if input.SoundEffectModels == nil {
		input.SoundEffectModels = []governance.SoundEffectModel{}
	}
	encrypted := current.EncryptedKey
	if encrypted == nil {
		encrypted = []byte{}
	}
	if input.ClearAPIKey {
		encrypted = []byte{}
	} else if input.APIKey != "" && input.APIKey != "********" {
		var err error
		encrypted, err = repo.EncryptSecret(input.APIKey)
		if err != nil {
			return 503, nil, errors.New("model key encryption is not configured")
		}
	}
	status := 200
	version := current.Version + 1
	if id == "" {
		id = uuid.NewString()
		status = 201
		if _, err := tx.Exec(ctx, "INSERT INTO governance_model_profiles(id,workspace_id) VALUES($1,$2)", id, p.WorkspaceID); err != nil {
			return 0, nil, err
		}
	}
	versionID := uuid.NewString()
	chat, _ := json.Marshal(input.ChatModels)
	embedding, _ := json.Marshal(input.EmbeddingModels)
	video, _ := json.Marshal(input.VideoModels)
	image, _ := json.Marshal(input.ImageModels)
	speech, _ := json.Marshal(input.SpeechModels)
	music, _ := json.Marshal(input.MusicModels)
	soundEffects, _ := json.Marshal(input.SoundEffectModels)
	if _, err := tx.Exec(ctx, "INSERT INTO governance_model_profile_versions(id,workspace_id,profile_id,version,name,provider,base_url,encrypted_key,chat_models,embedding_models,max_tokens,video_models,image_models,speech_models,music_models,sound_effect_models) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)", versionID, p.WorkspaceID, id, version, input.Name, input.Provider, input.BaseURL, encrypted, chat, embedding, input.MaxTokens, video, image, speech, music, soundEffects); err != nil {
		return 0, nil, err
	}
	if _, err := tx.Exec(ctx, "UPDATE governance_model_profiles SET current_version=$3 WHERE workspace_id=$1 AND id=$2", p.WorkspaceID, id, version); err != nil {
		return 0, nil, err
	}
	if err := repo.writeAudit(ctx, tx, p, "governance.model_profile.updated", "model_profile", id); err != nil {
		return 0, nil, err
	}
	if err := tx.Commit(ctx); err != nil {
		return 0, nil, err
	}
	profile := governance.ModelProfile{ID: id, VersionID: versionID, Version: version, Name: input.Name, Provider: input.Provider, BaseURL: input.BaseURL, HasAPIKey: len(encrypted) > 0, ChatModels: input.ChatModels, EmbeddingModels: input.EmbeddingModels, VideoModels: input.VideoModels, ImageModels: input.ImageModels, SpeechModels: input.SpeechModels, MusicModels: input.MusicModels, SoundEffectModels: input.SoundEffectModels, MaxTokens: input.MaxTokens}
	if profile.HasAPIKey {
		profile.APIKey = "********"
	}
	return status, profile, nil
}

func (repo *Repository) testModel(ctx context.Context, profile governance.ModelProfile, body json.RawMessage) (int, any, error) {
	var input governance.ModelTest
	if err := decodeModelTest(body, &input); err != nil {
		return 400, nil, err
	}
	secret, err := repo.DecryptSecret(profile.EncryptedKey)
	if err != nil {
		return 503, nil, errors.New("model key unavailable")
	}
	profile.APIKey = secret
	if repo.tester == nil {
		return 503, nil, errors.New("model tester unavailable")
	}
	result, err := repo.tester.Test(ctx, profile, input)
	if err != nil {
		return 422, nil, err
	}
	return 200, result, nil
}
