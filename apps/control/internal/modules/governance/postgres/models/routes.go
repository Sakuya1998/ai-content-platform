package models

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/database"
	"github.com/jackc/pgx/v5"
)

func (repo *Repository) FreezeTarget(ctx context.Context, tx pgx.Tx, workspace, kind string, target RouteTarget) (RouteTarget, error) {
	if target.ModelName == "" || target.ProfileID == "" {
		return target, errors.New("profile and model required")
	}
	profile, err := ScanModelProfile(tx.QueryRow(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND p.id::text=$2 AND NOT p.deleted AND v.version=p.current_version FOR SHARE OF p", workspace, target.ProfileID))
	if err != nil {
		return target, err
	}
	available := profile.ChatModels
	switch kind {
	case "embedding":
		available = profile.EmbeddingModels
	case "video_generation":
		if _, ok := governance.VideoModelCapabilities(profile.VideoModels, target.ModelName); !ok {
			return target, errors.New("video model is not in profile")
		}
	case "image_generation":
		if _, ok := governance.ImageModelCapabilities(profile.ImageModels, target.ModelName); !ok {
			return target, errors.New("image model is not in profile")
		}
	case "speech_synthesis":
		if _, ok := governance.SpeechModelCapabilities(profile.SpeechModels, target.ModelName); !ok {
			return target, errors.New("speech model is not in profile")
		}
	case "music_generation":
		if _, ok := governance.MusicModelCapabilities(profile.MusicModels, target.ModelName); !ok {
			return target, errors.New("music model is not in profile")
		}
	case "sound_effect_generation":
		if _, ok := governance.SoundEffectModelCapabilities(profile.SoundEffectModels, target.ModelName); !ok {
			return target, errors.New("sound effect model is not in profile")
		}
	default:
		if !governance.ModelContains(available, target.ModelName) {
			return target, errors.New("model is not in profile")
		}
	}
	target.ProfileVersionID = profile.VersionID
	if (target.FallbackProfileID == "") != (target.FallbackModelName == "") {
		return target, errors.New("fallback profile and model required")
	}
	if target.FallbackProfileID != "" {
		target.Fallback = append(target.Fallback, RouteTarget{ProfileID: target.FallbackProfileID, ModelName: target.FallbackModelName})
		target.FallbackProfileID = ""
		target.FallbackModelName = ""
	}
	if len(target.Fallback) > 4 {
		return target, errors.New("too many fallback routes")
	}
	for index, fallback := range target.Fallback {
		if len(fallback.Fallback) > 0 || fallback.FallbackProfileID != "" {
			return target, errors.New("nested fallback not supported")
		}
		target.Fallback[index], err = repo.FreezeTarget(ctx, tx, workspace, kind, fallback)
		if err != nil {
			return target, err
		}
	}
	return target, nil
}

func (repo *Repository) resolveTarget(ctx context.Context, tx pgx.Tx, workspace, kind string, target RouteTarget) (FrozenRoute, error) {
	profile, err := ScanModelProfile(tx.QueryRow(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND v.id::text=$2", workspace, target.ProfileVersionID))
	if err != nil {
		return FrozenRoute{}, err
	}
	secret, err := repo.DecryptSecret(profile.EncryptedKey)
	if err != nil {
		return FrozenRoute{}, err
	}
	base := profile.BaseURL
	if base == "" {
		base, _ = governance.ProviderURL(profile.Provider)
	}
	route := FrozenRoute{ProfileVersionID: profile.VersionID, Provider: profile.Provider, ModelName: target.ModelName, BaseURL: base, APIKey: secret, MaxTokens: profile.MaxTokens, Fallback: []FrozenRoute{}}
	if kind == "video_generation" {
		capabilities, ok := governance.VideoModelCapabilities(profile.VideoModels, target.ModelName)
		if !ok {
			return route, errors.New("video model is not in frozen profile")
		}
		route.VideoCapabilities = &capabilities
	}
	if kind == "image_generation" {
		capabilities, ok := governance.ImageModelCapabilities(profile.ImageModels, target.ModelName)
		if !ok {
			return route, errors.New("image model is not in frozen profile")
		}
		route.ImageCapabilities = &capabilities
	}
	if kind == "speech_synthesis" {
		capabilities, ok := governance.SpeechModelCapabilities(profile.SpeechModels, target.ModelName)
		if !ok {
			return route, errors.New("speech model is not in frozen profile")
		}
		route.SpeechCapabilities = &capabilities
	}
	if kind == "music_generation" {
		capabilities, ok := governance.MusicModelCapabilities(profile.MusicModels, target.ModelName)
		if !ok {
			return route, errors.New("music model is not in frozen profile")
		}
		route.MusicCapabilities = &capabilities
	}
	if kind == "sound_effect_generation" {
		capabilities, ok := governance.SoundEffectModelCapabilities(profile.SoundEffectModels, target.ModelName)
		if !ok {
			return route, errors.New("sound effect model is not in frozen profile")
		}
		route.SoundEffectCapabilities = &capabilities
	}
	for _, fallback := range target.Fallback {
		resolved, err := repo.resolveTarget(ctx, tx, workspace, kind, fallback)
		if err != nil {
			return route, err
		}
		route.Fallback = append(route.Fallback, resolved)
	}
	return route, nil
}

func (repo *Repository) ResolveFrozenRoutes(ctx context.Context, workspace, id string) (FrozenRoutes, error) {
	tx, err := database.BeginWorkspaceTx(ctx, repo.pool, workspace)
	if err != nil {
		return FrozenRoutes{}, err
	}
	defer tx.Rollback(ctx)
	result := FrozenRoutes{WorkspaceID: workspace, Routes: map[string]FrozenRoute{}}
	query := "SELECT id::text,version,routes FROM governance_model_routes WHERE workspace_id=$1 AND id::text=$2"
	args := []any{workspace, id}
	if id == "current" {
		query = "SELECT id::text,version,routes FROM governance_model_routes WHERE workspace_id=$1 ORDER BY version DESC LIMIT 1"
		args = []any{workspace}
	}
	var data []byte
	if err = tx.QueryRow(ctx, query, args...).Scan(&result.ID, &result.Version, &data); err != nil {
		return result, err
	}
	var targets map[string]RouteTarget
	if err = json.Unmarshal(data, &targets); err != nil {
		return result, err
	}
	for kind, target := range targets {
		result.Routes[kind], err = repo.resolveTarget(ctx, tx, workspace, kind, target)
		if err != nil {
			return result, err
		}
	}
	return result, nil
}

func (repo *Repository) ResolveFrozenRoute(ctx context.Context, workspace, versionID, kind string) (FrozenRoute, error) {
	routes, err := repo.ResolveFrozenRoutes(ctx, workspace, versionID)
	if err != nil {
		return FrozenRoute{}, err
	}
	route, ok := routes.Routes[kind]
	if !ok {
		return route, errors.New("route kind " + kind + " not configured")
	}
	return route, nil
}

func (repo *Repository) CurrentRouteVersion(ctx context.Context, workspace string) (string, error) {
	tx, err := database.BeginWorkspaceTx(ctx, repo.pool, workspace)
	if err != nil {
		return "", err
	}
	defer tx.Rollback(ctx)
	var id string
	err = tx.QueryRow(ctx, "SELECT id::text FROM governance_model_routes WHERE workspace_id=$1 ORDER BY version DESC LIMIT 1", workspace).Scan(&id)
	return id, err
}
