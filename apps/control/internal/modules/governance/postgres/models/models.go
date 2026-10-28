package models

import (
	"bytes"
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type ModelProfile = governance.ModelProfile
type RouteTarget = governance.RouteTarget
type FrozenRoute = governance.FrozenRoute
type FrozenRoutes = governance.FrozenRoutes
type Principal = identity.Principal

type ModelTester interface {
	Test(context.Context, governance.ModelProfile, governance.ModelTest) (map[string]any, error)
}

const modelProfileSelect = `SELECT p.id::text,v.id::text,v.version,v.name,v.provider,v.base_url,v.encrypted_key,v.chat_models,v.embedding_models,v.max_tokens,v.video_models,v.image_models,v.speech_models,v.music_models,v.sound_effect_models FROM governance_model_profiles p JOIN governance_model_profile_versions v ON v.profile_id=p.id `

type Repository struct {
	pool   *pgxpool.Pool
	key    []byte
	tester ModelTester
	audit  func(context.Context, pgx.Tx, Principal, string, string, string) error
}

func NewRepository(pool *pgxpool.Pool, encryptionKey string, tester ModelTester, writeAudit func(context.Context, pgx.Tx, Principal, string, string, string) error) *Repository {
	key, err := hex.DecodeString(encryptionKey)
	if err != nil || len(key) != 32 {
		key, err = base64.StdEncoding.DecodeString(encryptionKey)
	}
	if err != nil || len(key) != 32 {
		key = nil
	}
	return &Repository{pool: pool, key: key, tester: tester, audit: writeAudit}
}

func ScanModelProfile(row pgx.Row) (governance.ModelProfile, error) {
	var profile governance.ModelProfile
	var chat, embedding, video, image, speech, music, soundEffects []byte
	err := row.Scan(&profile.ID, &profile.VersionID, &profile.Version, &profile.Name, &profile.Provider, &profile.BaseURL, &profile.EncryptedKey, &chat, &embedding, &profile.MaxTokens, &video, &image, &speech, &music, &soundEffects)
	if err == nil {
		err = json.Unmarshal(chat, &profile.ChatModels)
	}
	if err == nil {
		err = json.Unmarshal(embedding, &profile.EmbeddingModels)
	}
	if err == nil {
		err = json.Unmarshal(video, &profile.VideoModels)
	}
	if err == nil {
		err = json.Unmarshal(image, &profile.ImageModels)
	}
	if err == nil {
		err = json.Unmarshal(speech, &profile.SpeechModels)
	}
	if err == nil {
		err = json.Unmarshal(music, &profile.MusicModels)
	}
	if err == nil {
		err = json.Unmarshal(soundEffects, &profile.SoundEffectModels)
	}
	profile.HasAPIKey = len(profile.EncryptedKey) > 0
	if profile.HasAPIKey {
		profile.APIKey = "********"
	}
	return profile, err
}

func FrozenModelProfile(ctx context.Context, tx pgx.Tx, workspace, id string) (governance.ModelProfile, error) {
	return ScanModelProfile(tx.QueryRow(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND v.id::text=$2", workspace, id))
}

func (repo *Repository) EncryptSecret(secret string) ([]byte, error) {
	if secret == "" {
		return []byte{}, nil
	}
	if len(repo.key) != 32 {
		return nil, errors.New("model encryption key must encode 32 bytes")
	}
	block, err := aes.NewCipher(repo.key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err = rand.Read(nonce); err != nil {
		return nil, err
	}
	return aead.Seal(nonce, nonce, []byte(secret), []byte("novel-agent:model-key:v1")), nil
}

func (repo *Repository) DecryptSecret(encrypted []byte) (string, error) {
	if len(encrypted) == 0 {
		return "", nil
	}
	if len(repo.key) != 32 {
		return "", errors.New("model encryption key must encode 32 bytes")
	}
	block, err := aes.NewCipher(repo.key)
	if err != nil {
		return "", err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	if len(encrypted) < aead.NonceSize() {
		return "", errors.New("invalid model ciphertext")
	}
	secret, err := aead.Open(nil, encrypted[:aead.NonceSize()], encrypted[aead.NonceSize():], []byte("novel-agent:model-key:v1"))
	return string(secret), err
}

func (repo *Repository) writeAudit(ctx context.Context, tx pgx.Tx, principal Principal, action, resourceType, resourceID string) error {
	if repo.audit == nil {
		return errors.New("audit writer unavailable")
	}
	return repo.audit(ctx, tx, principal, action, resourceType, resourceID)
}

func decodeModelProfileWrite(body json.RawMessage, value *governance.ModelProfileWrite) error {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return errors.New("invalid request body")
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return errors.New("request body must contain one JSON object")
	}
	return nil
}

func decodeModelTest(body json.RawMessage, value *governance.ModelTest) error {
	decoder := json.NewDecoder(bytes.NewReader(body))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return errors.New("invalid request body")
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		return errors.New("request body must contain one JSON object")
	}
	return nil
}
