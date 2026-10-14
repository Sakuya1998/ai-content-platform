package governance

import (
	"encoding/json"
	"errors"
	"math"
	"reflect"
	"strings"
	"unicode/utf8"
)

var imageFormats = []string{"png", "jpeg", "webp"}

var speechFormats = []string{"mp3", "wav", "opus", "aac", "flac", "pcm"}

func validMediaString(value string, maximum int) bool {
	return strings.TrimSpace(value) != "" && strings.TrimSpace(value) == value && utf8.RuneCountInString(value) <= maximum
}

func validMediaList(values []string, maximumItems, maximumLength int, allowed []string) bool {
	if len(values) == 0 || len(values) > maximumItems {
		return false
	}
	seen := map[string]bool{}
	for _, value := range values {
		if !validMediaString(value, maximumLength) || seen[value] || allowed != nil && !modelContains(allowed, value) {
			return false
		}
		seen[value] = true
	}
	return true
}

func validateImageModels(models []ImageModel) error {
	if len(models) > 50 {
		return errors.New("too many image models")
	}
	names := map[string]bool{}
	for _, model := range models {
		c := model.Capabilities
		if !validMediaString(model.ModelName, 200) || names[model.ModelName] {
			return errors.New("invalid or duplicate image model name")
		}
		names[model.ModelName] = true
		if !c.Generation && !c.Editing || !validMediaList(c.Sizes, 50, 32, nil) || !validMediaList(c.Formats, 3, 8, imageFormats) || c.MaxImages < 1 || c.MaxImages > 4 {
			return errors.New("invalid image model capabilities")
		}
	}
	return nil
}

func validateSpeechModels(models []SpeechModel) error {
	if len(models) > 50 {
		return errors.New("too many speech models")
	}
	names := map[string]bool{}
	for _, model := range models {
		c := model.Capabilities
		if !validMediaString(model.ModelName, 200) || names[model.ModelName] {
			return errors.New("invalid or duplicate speech model name")
		}
		names[model.ModelName] = true
		if !validMediaList(c.Voices, 100, 200, nil) || !validMediaList(c.Formats, 6, 8, speechFormats) || !validMediaList(c.Languages, 100, 32, nil) || !boundedMediaReal(c.MinSpeed, 0.25, 4) || !boundedMediaReal(c.MaxSpeed, c.MinSpeed, 4) || c.MaxInputChars < 1 || c.MaxInputChars > 16000 {
			return errors.New("invalid speech model capabilities")
		}
	}
	return nil
}

func imageModelCapabilities(models []ImageModel, name string) (ImageCapabilities, bool) {
	for _, model := range models {
		if model.ModelName == name {
			return model.Capabilities, true
		}
	}
	return ImageCapabilities{}, false
}

func speechModelCapabilities(models []SpeechModel, name string) (SpeechCapabilities, bool) {
	for _, model := range models {
		if model.ModelName == name {
			return model.Capabilities, true
		}
	}
	return SpeechCapabilities{}, false
}

func boundedMediaReal(value any, minimum, maximum float64) bool {
	var number float64
	if v, ok := value.(json.Number); ok {
		parsed, err := v.Float64()
		if err != nil {
			return false
		}
		number = parsed
	} else {
		v := reflect.ValueOf(value)
		if !v.IsValid() {
			return false
		}
		switch v.Kind() {
		case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
			number = float64(v.Int())
		case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
			number = float64(v.Uint())
		case reflect.Float32, reflect.Float64:
			number = v.Float()
		default:
			return false
		}
	}
	return !math.IsNaN(number) && !math.IsInf(number, 0) && number >= minimum && number <= maximum
}

func mediaArguments(arguments map[string]any, key string) (map[string]any, bool) {
	for field := range arguments {
		if field != key {
			return nil, false
		}
	}
	value, exists := arguments[key]
	if !exists {
		return nil, true
	}
	parameters, ok := value.(map[string]any)
	return parameters, ok
}

func validImageArguments(arguments map[string]any, sourceVersionID string) bool {
	parameters, ok := mediaArguments(arguments, "image")
	if !ok {
		return false
	}
	for key, value := range parameters {
		switch key {
		case "operation":
			operation, ok := value.(string)
			if !ok || operation != "generate" && operation != "edit" || operation == "edit" && sourceVersionID == "" {
				return false
			}
		case "n":
			if !boundedVideoInteger(value, 1, 4) {
				return false
			}
		case "size":
			text, ok := value.(string)
			if !ok || !validMediaString(text, 32) {
				return false
			}
		case "format":
			text, ok := value.(string)
			if !ok || !modelContains(imageFormats, text) {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func validSpeechArguments(arguments map[string]any) bool {
	parameters, ok := mediaArguments(arguments, "audio")
	if !ok {
		return false
	}
	for key, value := range parameters {
		switch key {
		case "speed":
			if !boundedMediaReal(value, 0.25, 4) {
				return false
			}
		case "format":
			text, ok := value.(string)
			if !ok || !modelContains(speechFormats, text) {
				return false
			}
		case "voice", "language", "character_id", "scene_id":
			text, ok := value.(string)
			maximum := 200
			if key == "language" {
				maximum = 32
			}
			if !ok || !validMediaString(text, maximum) {
				return false
			}
		case "text", "instructions":
			text, ok := value.(string)
			if !ok || utf8.RuneCountInString(text) > 16000 || key == "text" && strings.TrimSpace(text) == "" {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func validRouteKind(kind string) bool {
	switch kind {
	case "creative", "analysis", "embedding", "video_generation", "image_generation", "speech_synthesis", "music_generation", "sound_effect_generation":
		return true
	}
	return false
}

var ValidMediaString = validMediaString

var ValidMediaList = validMediaList

var ValidateImageModels = validateImageModels

var ValidateSpeechModels = validateSpeechModels

var ImageModelCapabilities = imageModelCapabilities

var SpeechModelCapabilities = speechModelCapabilities

var BoundedMediaReal = boundedMediaReal

var MediaArguments = mediaArguments

var ValidImageArguments = validImageArguments

var ValidSpeechArguments = validSpeechArguments

var ValidRouteKind = validRouteKind
