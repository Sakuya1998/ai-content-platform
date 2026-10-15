package models

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
)

// ModelProfile represents a model provider configuration
type ModelProfile struct {
	ID                 string                 `json:"id"`
	VersionID          string                 `json:"version_id"`
	Version            int                    `json:"version"`
	Name               string                 `json:"name"`
	Provider           string                 `json:"provider"`
	BaseURL            string                 `json:"base_url,omitempty"`
	EncryptedKey       []byte                 `json:"-"`
	HasAPIKey          bool                   `json:"has_api_key"`
	ChatModels         []map[string]any       `json:"chat_models"`
	EmbeddingModels    []map[string]any       `json:"embedding_models"`
	MaxTokens          int                    `json:"max_tokens"`
	VideoModels        []map[string]any       `json:"video_models,omitempty"`
	ImageModels        []map[string]any       `json:"image_models,omitempty"`
	SpeechModels       []map[string]any       `json:"speech_models,omitempty"`
	MusicModels        []map[string]any       `json:"music_models,omitempty"`
	SoundEffectModels  []map[string]any       `json:"sound_effect_models,omitempty"`
}

// RouteTarget identifies a specific model within a profile
type RouteTarget struct {
	ProfileID string `json:"profile_id"`
	ModelName string `json:"model_name"`
}

const modelProfileSelect = `SELECT p.id::text,v.id::text,v.version,v.name,v.provider,v.base_url,v.encrypted_key,v.chat_models,v.embedding_models,v.max_tokens,v.video_models,v.image_models,v.speech_models,v.music_models,v.sound_effect_models FROM governance_model_profiles p JOIN governance_model_profile_versions v ON v.profile_id=p.id `

// ScanModelProfile reads a ModelProfile from a database row
func ScanModelProfile(row pgx.Row) (ModelProfile, error) {
	var p ModelProfile
	var chat, embedding, video, image, speech, music, soundEffects []byte
	err := row.Scan(&p.ID, &p.VersionID, &p.Version, &p.Name, &p.Provider, &p.BaseURL, &p.EncryptedKey, &chat, &embedding, &p.MaxTokens, &video, &image, &speech, &music, &soundEffects)
	if err == nil {
		err = json.Unmarshal(chat, &p.ChatModels)
	}
	if err == nil {
		err = json.Unmarshal(embedding, &p.EmbeddingModels)
	}
	if err == nil && len(video) > 0 {
		err = json.Unmarshal(video, &p.VideoModels)
	}
	if err == nil && len(image) > 0 {
		err = json.Unmarshal(image, &p.ImageModels)
	}
	if err == nil && len(speech) > 0 {
		err = json.Unmarshal(speech, &p.SpeechModels)
	}
	if err == nil && len(music) > 0 {
		err = json.Unmarshal(music, &p.MusicModels)
	}
	if err == nil {
		err = json.Unmarshal(soundEffects, &p.SoundEffectModels)
	}
	p.HasAPIKey = len(p.EncryptedKey) > 0
	return p, err
}

// Principal represents the authenticated user context
type Principal struct {
	WorkspaceID string
	UserID      string
}

// Repository provides model profile CRUD and routing operations
type Repository struct {
	pool interface {
		Begin(context.Context) (pgx.Tx, error)
	}
	writeAudit func(context.Context, pgx.Tx, Principal, string, string, string) error
}

// NewRepository creates a new model repository
func NewRepository(
	pool interface {
		Begin(context.Context) (pgx.Tx, error)
	},
	writeAudit func(context.Context, pgx.Tx, Principal, string, string, string) error,
) *Repository {
	return &Repository{
		pool:       pool,
		writeAudit: writeAudit,
	}
}

// FreezeTarget resolves and freezes a route target for execution
func (repo *Repository) FreezeTarget(ctx context.Context, tx pgx.Tx, workspace, kind string, target RouteTarget) (RouteTarget, error) {
	if target.ModelName == "" || target.ProfileID == "" {
		return RouteTarget{}, governance.ErrInvalidTarget
	}
	var version int
	err := tx.QueryRow(ctx, "SELECT v.version FROM governance_model_profiles p JOIN governance_model_profile_versions v ON v.profile_id=p.id WHERE p.workspace_id=$1 AND p.id::text=$2 AND v.version=p.current_version", workspace, target.ProfileID).Scan(&version)
	if err != nil {
		return RouteTarget{}, err
	}
	var column string
	switch kind {
	case "chat":
		column = "chat_models"
	case "embedding":
		column = "embedding_models"
	case "video":
		column = "video_models"
	case "image":
		column = "image_models"
	case "speech":
		column = "speech_models"
	case "music":
		column = "music_models"
	case "sound_effect":
		column = "sound_effect_models"
	default:
		return RouteTarget{}, governance.ErrInvalidKind
	}
	var modelsJSON []byte
	err = tx.QueryRow(ctx, "SELECT v."+column+" FROM governance_model_profiles p JOIN governance_model_profile_versions v ON v.profile_id=p.id WHERE p.workspace_id=$1 AND p.id::text=$2 AND v.version=$3", workspace, target.ProfileID, version).Scan(&modelsJSON)
	if err != nil {
		return RouteTarget{}, err
	}
	var models []map[string]any
	if err = json.Unmarshal(modelsJSON, &models); err != nil {
		return RouteTarget{}, err
	}
	found := false
	for _, m := range models {
		if name, ok := m["name"].(string); ok && name == target.ModelName {
			found = true
			break
		}
	}
	if !found {
		return RouteTarget{}, governance.ErrModelNotFound
	}
	return target, nil
}

// ExecuteModelCatalogOp handles CRUD operations for model profiles via catalog protocol
func (repo *Repository) ExecuteModelCatalogOp(
	ctx context.Context,
	p Principal,
	parts []string,
	action governance.Action,
	filters map[string]string,
	body json.RawMessage,
) (statusCode int, result any, err error) {
	tx, err := repo.pool.Begin(ctx)
	if err != nil {
		return 0, nil, err
	}
	defer tx.Rollback(ctx)

	_, err = tx.Exec(ctx, "SET LOCAL rls.workspace_id = $1", p.WorkspaceID)
	if err != nil {
		return 0, nil, err
	}

	// List profiles
	if len(parts) == 0 && action == governance.Read {
		rows, e := tx.Query(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND v.version=p.current_version ORDER BY v.name", p.WorkspaceID)
		if e != nil {
			return 0, nil, e
		}
		defer rows.Close()
		items := []ModelProfile{}
		for rows.Next() {
			item, e := ScanModelProfile(rows)
			if e != nil {
				return 0, nil, e
			}
			items = append(items, item)
		}
		if e = rows.Err(); e != nil {
			return 0, nil, e
		}
		return 200, map[string]any{"items": items, "has_more": false}, tx.Commit(ctx)
	}

	// Get or update specific profile
	if len(parts) == 1 {
		id := parts[0]
		if action == governance.Read {
			profile, e := ScanModelProfile(tx.QueryRow(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND p.id::text=$2 AND v.version=p.current_version", p.WorkspaceID, id))
			if e != nil {
				return 0, nil, e
			}
			return 200, profile, tx.Commit(ctx)
		}
	}

	// Create new profile
	if len(parts) == 0 && action == governance.Create {
		var input struct {
			Name              string           `json:"name"`
			Provider          string           `json:"provider"`
			BaseURL           string           `json:"base_url"`
			APIKey            string           `json:"api_key"`
			ChatModels        []map[string]any `json:"chat_models"`
			EmbeddingModels   []map[string]any `json:"embedding_models"`
			MaxTokens         int              `json:"max_tokens"`
			VideoModels       []map[string]any `json:"video_models"`
			ImageModels       []map[string]any `json:"image_models"`
			SpeechModels      []map[string]any `json:"speech_models"`
			MusicModels       []map[string]any `json:"music_models"`
			SoundEffectModels []map[string]any `json:"sound_effect_models"`
		}
		if err := json.Unmarshal(body, &input); err != nil {
			return 422, nil, err
		}

		profileID := uuid.NewString()
		versionID := uuid.NewString()

		var encryptedKey []byte
		// TODO: Encrypt API key if provided

		chatJSON, _ := json.Marshal(input.ChatModels)
		embeddingJSON, _ := json.Marshal(input.EmbeddingModels)
		videoJSON, _ := json.Marshal(input.VideoModels)
		imageJSON, _ := json.Marshal(input.ImageModels)
		speechJSON, _ := json.Marshal(input.SpeechModels)
		musicJSON, _ := json.Marshal(input.MusicModels)
		soundEffectJSON, _ := json.Marshal(input.SoundEffectModels)

		digest := sha256.Sum256(append(chatJSON, embeddingJSON...))
		contentHash := hex.EncodeToString(digest[:])

		_, err = tx.Exec(ctx, "INSERT INTO governance_model_profiles(id,workspace_id,current_version) VALUES($1,$2,1)", profileID, p.WorkspaceID)
		if err == nil {
			_, err = tx.Exec(ctx, "INSERT INTO governance_model_profile_versions(id,profile_id,version,name,provider,base_url,encrypted_key,chat_models,embedding_models,max_tokens,video_models,image_models,speech_models,music_models,sound_effect_models,content_hash,created_by,created_at) VALUES($1,$2,1,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)",
				versionID, profileID, input.Name, input.Provider, input.BaseURL, encryptedKey, chatJSON, embeddingJSON, input.MaxTokens, videoJSON, imageJSON, speechJSON, musicJSON, soundEffectJSON, contentHash, p.UserID, time.Now().UTC())
		}
		if err == nil {
			err = repo.writeAudit(ctx, tx, p, "governance.model_profile.created", "model_profiles", profileID)
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			return 0, nil, err
		}

		profile := ModelProfile{
			ID:                profileID,
			VersionID:         versionID,
			Version:           1,
			Name:              input.Name,
			Provider:          input.Provider,
			BaseURL:           input.BaseURL,
			ChatModels:        input.ChatModels,
			EmbeddingModels:   input.EmbeddingModels,
			MaxTokens:         input.MaxTokens,
			VideoModels:       input.VideoModels,
			ImageModels:       input.ImageModels,
			SpeechModels:      input.SpeechModels,
			MusicModels:       input.MusicModels,
			SoundEffectModels: input.SoundEffectModels,
			HasAPIKey:         len(encryptedKey) > 0,
		}
		return 201, profile, nil
	}

	return 405, nil, governance.ErrMethodNotAllowed
}
