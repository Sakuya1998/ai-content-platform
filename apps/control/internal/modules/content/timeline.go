package content

import (
	"encoding/json"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"regexp"
	"strings"
	"unicode"

	"github.com/google/uuid"
)

var boundedVideoInteger = governance.BoundedVideoInteger
var boundedMediaReal = governance.BoundedMediaReal

// Exact interoperability with JavaScript/JSON numbers, independent of render duration.
const TimelineSafeInteger = 1<<53 - 1

func ClosedObject(value any, fields ...string) (map[string]any, bool) {
	object, ok := value.(map[string]any)
	if !ok || object == nil {
		return nil, false
	}
	allowed := map[string]bool{}
	for _, field := range fields {
		allowed[field] = true
	}
	for field := range object {
		if !allowed[field] {
			return nil, false
		}
	}
	return object, true
}
func ValidStoryboardArguments(arguments map[string]any) bool {
	if len(arguments) == 0 {
		return true
	}
	outer, ok := ClosedObject(arguments, "storyboard")
	if !ok {
		return false
	}
	if _, exists := outer["storyboard"]; !exists {
		return true
	}
	parameters, ok := ClosedObject(outer["storyboard"], "target_duration_ms", "max_shots")
	if !ok {
		return false
	}
	for key, value := range parameters {
		maximum := float64(600000)
		if key == "max_shots" {
			maximum = 128
		}
		if !boundedVideoInteger(value, 1, maximum) {
			return false
		}
	}
	return true
}
func ValidComposeArguments(arguments map[string]any) bool {
	if len(arguments) == 0 {
		return true
	}
	outer, ok := ClosedObject(arguments, "compose")
	if !ok {
		return false
	}
	parameters, ok := ClosedObject(outer["compose"], "preset")
	if !ok {
		return false
	}
	if value, exists := parameters["preset"]; exists {
		return value == "draft" || value == "standard"
	}
	return true
}

// The manifest's immutable references must be frozen into this execution;
// providers and later document revisions cannot supply missing inputs.
func ValidComposeSource(document GeneratedDocument, inputs []string) bool {
	if document.DocumentType != "timeline" || document.SchemaVersion != "1.0" {
		return false
	}
	manifest, ok := document.Metadata["timeline"].(map[string]any)
	if !ok || len(manifest) == 0 || !ValidTimelineArguments(map[string]any{"timeline": manifest}) {
		return false
	}
	frozen := make(map[string]bool, len(inputs))
	for _, id := range inputs {
		frozen[id] = true
	}
	for _, value := range manifest["assets"].([]any) {
		asset := value.(map[string]any)
		if !frozen[asset["document_version_id"].(string)] {
			return false
		}
	}
	return true
}
func TimelineInteger(object map[string]any, field string, minimum, maximum, fallback int) (int, bool) {
	value, exists := object[field]
	if !exists {
		return fallback, fallback >= minimum && fallback <= maximum
	}
	if !boundedVideoInteger(value, float64(minimum), float64(maximum)) {
		return 0, false
	}
	raw, err := json.Marshal(value)
	if err != nil {
		return 0, false
	}
	var number float64
	if json.Unmarshal(raw, &number) != nil {
		return 0, false
	}
	return int(number), true
}
func TimelineText(object map[string]any, field string, maximum int) (string, bool) {
	text, ok := object[field].(string)
	return text, ok && strings.TrimSpace(text) != "" && len([]rune(text)) <= maximum
}
func TimelineList(object map[string]any, field string, minimum, maximum int) ([]any, bool) {
	value, exists := object[field]
	if !exists {
		return []any{}, minimum == 0
	}
	values, ok := value.([]any)
	return values, ok && len(values) >= minimum && len(values) <= maximum
}
func TimelineID(object map[string]any, field string, seen map[string]bool) bool {
	id, ok := TimelineText(object, field, 128)
	if !ok || !TimelineIdentifier.MatchString(id) || seen[id] {
		return false
	}
	seen[id] = true
	return true
}

var TimelineIdentifier = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)

func TimelineOptionalEnum(object map[string]any, field string, choices ...string) bool {
	value, exists := object[field]
	if !exists {
		return true
	}
	text, ok := value.(string)
	if !ok {
		return false
	}
	for _, choice := range choices {
		if text == choice {
			return true
		}
	}
	return false
}
func ValidTimelineArguments(arguments map[string]any) bool {
	if len(arguments) == 0 {
		return true
	}
	outer, ok := ClosedObject(arguments, "timeline")
	if !ok {
		return false
	}
	if _, exists := outer["timeline"]; !exists {
		return true
	}
	object, ok := ClosedObject(outer["timeline"], "schema_version", "width", "height", "fps", "duration_ms", "assets", "shots", "audio_tracks", "subtitles")
	if !ok {
		return false
	}
	if len(object) == 0 {
		return true
	}
	return ValidTimeline(object)
}
func ValidTimeline(object map[string]any) bool {
	if value, exists := object["schema_version"]; exists && value != "timeline@1" {
		return false
	}
	width, a := TimelineInteger(object, "width", 64, 1920, 0)
	height, b := TimelineInteger(object, "height", 64, 1920, 0)
	_, c := TimelineInteger(object, "fps", 1, 60, 0)
	duration, d := TimelineInteger(object, "duration_ms", 1, 600000, 0)
	if !a || !b || !c || !d || width%2 != 0 || height%2 != 0 || width*height > 2073600 {
		return false
	}
	assets, ok := TimelineList(object, "assets", 1, 256)
	if !ok {
		return false
	}
	assetIDs := map[string]bool{}
	for _, item := range assets {
		asset, ok := ClosedObject(item, "asset_id", "document_version_id", "block_position")
		if !ok || !TimelineID(asset, "asset_id", assetIDs) {
			return false
		}
		version, ok := asset["document_version_id"].(string)
		if !ok {
			return false
		}
		if parsed, err := uuid.Parse(version); err != nil || parsed.String() != version {
			return false
		}
		if _, ok := TimelineInteger(asset, "block_position", 0, TimelineSafeInteger, -1); !ok {
			return false
		}
	}
	shots, ok := TimelineList(object, "shots", 1, 128)
	if !ok {
		return false
	}
	shotIDs := map[string]bool{}
	type span struct{ start, duration int }
	spans := []span{}
	for _, item := range shots {
		shot, ok := ClosedObject(item, "shot_id", "asset_id", "start_ms", "duration_ms", "source_start_ms", "fit")
		if !ok || !TimelineID(shot, "shot_id", shotIDs) || !TimelineOptionalEnum(shot, "fit", "contain", "cover") {
			return false
		}
		asset, ok := shot["asset_id"].(string)
		if !ok || !assetIDs[asset] {
			return false
		}
		start, a := TimelineInteger(shot, "start_ms", 0, duration, -1)
		length, b := TimelineInteger(shot, "duration_ms", 1, duration, 0)
		_, c := TimelineInteger(shot, "source_start_ms", 0, TimelineSafeInteger, 0)
		if !a || !b || !c || start+length > duration {
			return false
		}
		spans = append(spans, span{start, length})
	}
	cursor := 0
	for _, span := range spans {
		if span.start != cursor {
			return false
		}
		cursor += span.duration
	}
	if cursor != duration {
		return false
	}
	tracks, ok := TimelineList(object, "audio_tracks", 0, 128)
	if !ok {
		return false
	}
	trackIDs := map[string]bool{}
	for _, item := range tracks {
		track, ok := ClosedObject(item, "track_id", "asset_id", "start_ms", "duration_ms", "source_start_ms", "gain", "role", "fade_in_ms", "fade_out_ms")
		if !ok || !TimelineID(track, "track_id", trackIDs) || !TimelineOptionalEnum(track, "role", "narration", "music", "effect") {
			return false
		}
		asset, ok := track["asset_id"].(string)
		if !ok || !assetIDs[asset] {
			return false
		}
		start, a := TimelineInteger(track, "start_ms", 0, duration, -1)
		length, b := TimelineInteger(track, "duration_ms", 1, duration, 0)
		_, c := TimelineInteger(track, "source_start_ms", 0, TimelineSafeInteger, 0)
		fadeIn, e := TimelineInteger(track, "fade_in_ms", 0, length, 0)
		fadeOut, f := TimelineInteger(track, "fade_out_ms", 0, length, 0)
		if !a || !b || !c || !e || !f || fadeIn+fadeOut > length || start+length > duration {
			return false
		}
		if gain, exists := track["gain"]; exists {
			if !boundedMediaReal(gain, 0, 2) {
				return false
			}
		}
	}
	cues, ok := TimelineList(object, "subtitles", 0, 256)
	if !ok {
		return false
	}
	cueIDs := map[string]bool{}
	for _, item := range cues {
		cue, ok := ClosedObject(item, "cue_id", "start_ms", "end_ms", "text")
		if !ok || !TimelineID(cue, "cue_id", cueIDs) {
			return false
		}
		start, a := TimelineInteger(cue, "start_ms", 0, duration, -1)
		end, b := TimelineInteger(cue, "end_ms", 1, duration, 0)
		if !a || !b || end <= start {
			return false
		}
		text, ok := TimelineText(cue, "text", 2000)
		if !ok {
			return false
		}
		for _, character := range text {
			if unicode.Is(unicode.Cc, character) || unicode.Is(unicode.Cf, character) || unicode.Is(unicode.Cs, character) {
				return false
			}
		}
	}
	return true
}
