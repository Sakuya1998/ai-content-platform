package postgres

import (
	"bytes"
	"encoding/json"
	"errors"

	"github.com/jackc/pgx/v5"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/content"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/execution"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/governance"
	productionmodule "github.com/sakuya/novel-agent/go-control-plane/internal/modules/production"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/project"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/task"
)

type GenerationRequest = execution.Request
type GeneratedDocument = content.GeneratedDocument
type ContentVersion = content.ContentVersion
type SnapshotFreezer = execution.SnapshotFreezer
type ModelProfile = governance.ModelProfile
type RouteTarget = governance.RouteTarget
type VideoCapabilities = governance.VideoCapabilities

var ErrMessageConflict = task.ErrConflict
var ErrNotFound = project.ErrNotFound

var normalizeProduction = productionmodule.Normalize
var validProduction = productionmodule.Valid
var storyboardShots = productionmodule.StoryboardShots
var validAssembleArguments = productionmodule.ValidAssembleArguments
var validAssembleSource = productionmodule.ValidAssembleSource
var validComposeArguments = content.ValidComposeArguments
var validComposeSource = content.ValidComposeSource
var timelineInteger = content.TimelineInteger

var boundedVideoInteger = governance.BoundedVideoInteger
var videoModelCapabilities = governance.VideoModelCapabilities
var imageModelCapabilities = governance.ImageModelCapabilities
var speechModelCapabilities = governance.SpeechModelCapabilities
var musicModelCapabilities = governance.MusicModelCapabilities
var soundEffectModelCapabilities = governance.SoundEffectModelCapabilities
var modelContains = governance.ModelContains
var boundedMediaReal = governance.BoundedMediaReal

func normalizeRepositoryError(err error) error {
	if errors.Is(err, pgx.ErrNoRows) {
		return ErrNotFound
	}
	return err
}

func sameJSON(left, right []byte) bool {
	var first, second any
	if json.Unmarshal(left, &first) != nil || json.Unmarshal(right, &second) != nil {
		return false
	}
	canonicalFirst, err := json.Marshal(first)
	if err != nil {
		return false
	}
	canonicalSecond, err := json.Marshal(second)
	return err == nil && bytes.Equal(canonicalFirst, canonicalSecond)
}
