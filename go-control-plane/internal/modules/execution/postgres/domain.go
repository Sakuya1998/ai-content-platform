package postgres

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"github.com/jackc/pgx/v5"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/content"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/execution"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/governance"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/project"
)

var ErrNotFound = project.ErrNotFound

type SnapshotFreezer = execution.SnapshotFreezer
type ModelProfile = governance.ModelProfile
type RouteTarget = governance.RouteTarget
type VideoCapabilities = governance.VideoCapabilities

var validVideoArguments = governance.ValidVideoArguments
var validImageArguments = governance.ValidImageArguments
var validSpeechArguments = governance.ValidSpeechArguments
var validMusicArguments = governance.ValidMusicArguments
var validSoundEffectArguments = governance.ValidSoundEffectArguments
var validAudioGenerationArguments = governance.ValidAudioGenerationArguments
var boundedVideoInteger = governance.BoundedVideoInteger
var videoModelCapabilities = governance.VideoModelCapabilities
var imageModelCapabilities = governance.ImageModelCapabilities
var speechModelCapabilities = governance.SpeechModelCapabilities
var musicModelCapabilities = governance.MusicModelCapabilities
var soundEffectModelCapabilities = governance.SoundEffectModelCapabilities
var modelContains = governance.ModelContains
var boundedMediaReal = governance.BoundedMediaReal
var validGeneration = execution.ValidGeneration
var validComposeArguments = content.ValidComposeArguments
var validComposeSource = content.ValidComposeSource
var validStoryboardArguments = content.ValidStoryboardArguments
var validTimelineArguments = content.ValidTimelineArguments
var timelineIdentifier = content.TimelineIdentifier

const timelineSafeInteger = content.TimelineSafeInteger

var timelineInteger = content.TimelineInteger

func resourceHash(body []byte) string {
	sum := sha256.Sum256(body)
	return hex.EncodeToString(sum[:])
}

func operationProject(ctx context.Context, tx pgx.Tx, workspace, project string) error {
	var exists bool
	if err := tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM projects WHERE id=$1 AND workspace_id=$2)", project, workspace).Scan(&exists); err != nil {
		return err
	}
	if !exists {
		return ErrNotFound
	}
	return nil
}
func normalizeRepositoryError(err error) error {
	if err == pgx.ErrNoRows {
		return ErrNotFound
	}
	return err
}
