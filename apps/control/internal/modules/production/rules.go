package production

import (
	"github.com/google/uuid"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/content"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/governance"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/task"
	"strings"
	"unicode/utf8"
)

type ProductionRequest = Request
type ProductionBinding = Binding
type productionShot = Shot
type GeneratedDocument = content.GeneratedDocument

var ErrMessageConflict = task.ErrConflict
var validImageArguments = governance.ValidImageArguments
var validVideoArguments = governance.ValidVideoArguments
var validSpeechArguments = governance.ValidSpeechArguments
var validMusicArguments = governance.ValidMusicArguments
var validSoundEffectArguments = governance.ValidSoundEffectArguments
var boundedVideoInteger = governance.BoundedVideoInteger
var timelineIdentifier = content.TimelineIdentifier

const timelineSafeInteger = content.TimelineSafeInteger

var timelineList = content.TimelineList
var timelineID = content.TimelineID
var timelineText = content.TimelineText
var timelineInteger = content.TimelineInteger
var closedObject = content.ClosedObject

func Normalize(r *ProductionRequest) {
	if r.Width == 0 {
		r.Width = 1280
	}
	if r.Height == 0 {
		r.Height = 720
	}
	if r.FPS == 0 {
		r.FPS = 25
	}
	if r.VisualMode == "" {
		r.VisualMode = "image"
	}
}
func Valid(r ProductionRequest) bool {
	for _, id := range []string{r.ProjectID, r.StoryboardVersionID} {
		if _, err := uuid.Parse(id); err != nil {
			return false
		}
	}
	if strings.TrimSpace(r.Title) == "" || utf8.RuneCountInString(r.Title) > 120 || strings.TrimSpace(r.IdempotencyKey) == "" || len(r.IdempotencyKey) > 200 || r.VisualMode != "image" && r.VisualMode != "video" {
		return false
	}
	if r.Width < 64 || r.Width > 1920 || r.Height < 64 || r.Height > 1920 || r.Width%2 != 0 || r.Height%2 != 0 || r.Width*r.Height > 2073600 || r.FPS < 1 || r.FPS > 60 || len(r.Bindings) > 32 {
		return false
	}
	imageArgs, videoArgs, audioArgs, musicArgs, effectArgs := map[string]any{}, map[string]any{}, map[string]any{}, map[string]any{}, map[string]any{}
	if r.ImageParameters != nil {
		imageArgs["image"] = r.ImageParameters
	}
	if r.VideoParameters != nil {
		videoArgs["video"] = r.VideoParameters
	}
	if r.AudioParameters != nil {
		audioArgs["audio"] = r.AudioParameters
	}
	if r.MusicParameters != nil {
		musicArgs["music"] = r.MusicParameters
	}
	if r.SoundEffectParameters != nil {
		effectArgs["effect"] = r.SoundEffectParameters
	}
	if !validImageArguments(imageArgs, r.StoryboardVersionID) || !validVideoArguments(videoArgs) || !validSpeechArguments(audioArgs) || !validMusicArguments(musicArgs) || !validSoundEffectArguments(effectArgs) {
		return false
	}
	if operation, exists := r.ImageParameters["operation"]; exists && operation != "generate" {
		return false
	}
	if count, exists := r.ImageParameters["n"]; exists && !boundedVideoInteger(count, 1, 1) {
		return false
	}
	if _, exists := r.VideoParameters["image_url"]; exists {
		return false
	}
	for _, forbidden := range []string{"text"} {
		if _, ok := r.AudioParameters[forbidden]; ok {
			return false
		}
	}
	seen := map[string]bool{}
	for _, b := range r.Bindings {
		if !timelineIdentifier.MatchString(b.ShotID) || len(b.ShotID) > 128 || seen[b.ShotID] || b.VisualBlockPosition < 0 || b.AudioBlockPosition < 0 {
			return false
		}
		seen[b.ShotID] = true
		for _, id := range []string{b.VisualVersionID, b.AudioVersionID} {
			if id != "" {
				if _, e := uuid.Parse(id); e != nil {
					return false
				}
			}
		}
		if b.VisualVersionID == "" && b.VisualBlockPosition != 0 || b.AudioVersionID == "" && b.AudioBlockPosition != 0 {
			return false
		}
	}
	return true
}
func StoryboardShots(d GeneratedDocument) ([]productionShot, error) {
	if d.DocumentType != "storyboard" || d.SchemaVersion != "1.0" {
		return nil, ErrMessageConflict
	}
	manifest, ok := closedObject(d.Metadata["storyboard"], "schema_version", "shots")
	if !ok || manifest["schema_version"] != "storyboard@1" {
		return nil, ErrMessageConflict
	}
	items, ok := timelineList(manifest, "shots", 1, 32)
	if !ok {
		return nil, ErrMessageConflict
	}
	shots := []productionShot{}
	seen := map[string]bool{}
	total := 0
	for _, item := range items {
		obj, ok := closedObject(item, "shot_id", "description", "dialogue", "duration_ms", "source_block_ids")
		if !ok || !timelineID(obj, "shot_id", seen) {
			return nil, ErrMessageConflict
		}
		description, ok := timelineText(obj, "description", 8000)
		if !ok {
			return nil, ErrMessageConflict
		}
		duration, ok := timelineInteger(obj, "duration_ms", 1, 600000, 0)
		if !ok {
			return nil, ErrMessageConflict
		}
		total += duration
		dialogue := ""
		if v, present := obj["dialogue"]; present {
			var good bool
			dialogue, good = v.(string)
			if !good || utf8.RuneCountInString(dialogue) > 8000 {
				return nil, ErrMessageConflict
			}
		}
		if values, present := obj["source_block_ids"]; present {
			ids, ok := values.([]any)
			if !ok || len(ids) > 256 {
				return nil, ErrMessageConflict
			}
			for _, id := range ids {
				s, ok := id.(string)
				if !ok || !timelineIdentifier.MatchString(s) || len(s) > 128 {
					return nil, ErrMessageConflict
				}
			}
		}
		shots = append(shots, productionShot{ShotID: obj["shot_id"].(string), Description: description, Dialogue: dialogue, DurationMS: duration})
	}
	if total > 600000 {
		return nil, ErrMessageConflict
	}
	return shots, nil
}
func ValidAssembleArguments(arguments map[string]any) bool {
	outer, ok := closedObject(arguments, "assemble")
	if !ok {
		return false
	}
	obj, ok := closedObject(outer["assemble"], "width", "height", "fps", "subtitles", "shots", "music_version_id", "music_block_position", "effects")
	if !ok {
		return false
	}
	w, a := timelineInteger(obj, "width", 64, 1920, 1280)
	h, b := timelineInteger(obj, "height", 64, 1920, 720)
	_, c := timelineInteger(obj, "fps", 1, 60, 25)
	if !a || !b || !c || w%2 != 0 || h%2 != 0 || w*h > 2073600 {
		return false
	}
	if s, exists := obj["subtitles"]; exists {
		if _, ok := s.(bool); !ok {
			return false
		}
	}
	if music, exists := obj["music_version_id"]; exists {
		id, ok := music.(string)
		parsed, err := uuid.Parse(id)
		if !ok || err != nil || parsed.String() != id {
			return false
		}
	} else if _, exists := obj["music_block_position"]; exists {
		return false
	}
	if _, exists := obj["music_block_position"]; exists {
		if _, ok := timelineInteger(obj, "music_block_position", 0, timelineSafeInteger, -1); !ok {
			return false
		}
	}
	items, ok := timelineList(obj, "shots", 1, 32)
	if !ok {
		return false
	}
	seen := map[string]bool{}
	for _, item := range items {
		s, ok := closedObject(item, "shot_id", "visual_version_id", "visual_block_position", "audio_version_id", "audio_block_position")
		if !ok || !timelineID(s, "shot_id", seen) {
			return false
		}
		for _, field := range []string{"visual_version_id", "audio_version_id"} {
			id, exists := s[field]
			if !exists && field == "audio_version_id" {
				continue
			}
			text, ok := id.(string)
			if !ok {
				return false
			}
			if parsed, e := uuid.Parse(text); e != nil || parsed.String() != text {
				return false
			}
		}
		if _, ok := timelineInteger(s, "visual_block_position", 0, timelineSafeInteger, 0); !ok {
			return false
		}
		if _, ok := timelineInteger(s, "audio_block_position", 0, timelineSafeInteger, 0); !ok {
			return false
		}
		if _, exists := s["audio_block_position"]; exists {
			if _, ok := s["audio_version_id"]; !ok {
				return false
			}
		}
	}
	if effects, exists := obj["effects"]; exists {
		values, ok := timelineList(map[string]any{"effects": effects}, "effects", 0, 32)
		if !ok {
			return false
		}
		effectShots := map[string]bool{}
		for _, value := range values {
			effect, ok := closedObject(value, "shot_id", "version_id", "block_position")
			if !ok || !timelineID(effect, "shot_id", effectShots) {
				return false
			}
			if _, exists := seen[effect["shot_id"].(string)]; !exists {
				return false
			}
			id, ok := effect["version_id"].(string)
			parsed, err := uuid.Parse(id)
			if !ok || err != nil || parsed.String() != id {
				return false
			}
			if _, ok := timelineInteger(effect, "block_position", 0, timelineSafeInteger, -1); !ok {
				return false
			}
		}
	}
	return true
}
func ValidAssembleSource(d GeneratedDocument, args map[string]any, inputs []string) bool {
	shots, err := StoryboardShots(d)
	if err != nil || !ValidAssembleArguments(args) {
		return false
	}
	items := args["assemble"].(map[string]any)["shots"].([]any)
	if len(shots) != len(items) {
		return false
	}
	bound := map[string]bool{}
	for _, id := range inputs {
		bound[id] = true
	}
	for i, item := range items {
		s := item.(map[string]any)
		if s["shot_id"] != shots[i].ShotID {
			return false
		}
		for _, field := range []string{"visual_version_id", "audio_version_id"} {
			if id, exists := s[field]; exists && !bound[id.(string)] {
				return false
			}
		}
	}
	object := args["assemble"].(map[string]any)
	for _, field := range []string{"music_version_id"} {
		if id, exists := object[field]; exists && !bound[id.(string)] {
			return false
		}
	}
	if raw, exists := object["effects"]; exists {
		for _, item := range raw.([]any) {
			if !bound[item.(map[string]any)["version_id"].(string)] {
				return false
			}
		}
	}
	return true
}
