package governance

import (
	"encoding/json"
	"errors"
	"math"
	"net/url"
	"reflect"
	"strings"
)

func validateVideoModels(models []VideoModel) error {
	if len(models) > 50 {
		return errors.New("too many video models")
	}
	names := map[string]bool{}
	for _, model := range models {
		name := model.ModelName
		if strings.TrimSpace(name) == "" || strings.TrimSpace(name) != name || len(name) > 200 || names[name] {
			return errors.New("invalid or duplicate video model name")
		}
		names[name] = true
		capabilities := model.Capabilities
		if !capabilities.TextToVideo && !capabilities.ImageToVideo {
			return errors.New("video model must support text or image generation")
		}
		if len(capabilities.Durations) == 0 || len(capabilities.Durations) > 120 {
			return errors.New("video durations required")
		}
		durations := map[int]bool{}
		for _, duration := range capabilities.Durations {
			if duration < 1 || duration > 120 || durations[duration] {
				return errors.New("invalid or duplicate video duration")
			}
			durations[duration] = true
		}
		for _, values := range [][]string{capabilities.Ratios, capabilities.Resolutions} {
			if len(values) == 0 || len(values) > 50 {
				return errors.New("video ratios and resolutions required")
			}
			seen := map[string]bool{}
			for _, value := range values {
				if strings.TrimSpace(value) == "" || strings.TrimSpace(value) != value || len(value) > 32 || seen[value] {
					return errors.New("invalid or duplicate video ratio or resolution")
				}
				seen[value] = true
			}
		}
	}
	return nil
}

func videoModelCapabilities(models []VideoModel, name string) (VideoCapabilities, bool) {
	for _, model := range models {
		if model.ModelName == name {
			return model.Capabilities, true
		}
	}
	return VideoCapabilities{}, false
}

func boundedVideoInteger(value any, minimum, maximum float64) bool {
	var number float64
	if jsonNumber, ok := value.(json.Number); ok {
		parsed, err := jsonNumber.Float64()
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
	return !math.IsNaN(number) && !math.IsInf(number, 0) && math.Trunc(number) == number && number >= minimum && number <= maximum
}

func validVideoArguments(arguments map[string]any) bool {
	for key := range arguments {
		if key != "video" {
			return false
		}
	}
	value, exists := arguments["video"]
	if !exists {
		return true
	}
	parameters, ok := value.(map[string]any)
	if !ok {
		return false
	}
	for key, value := range parameters {
		switch key {
		case "duration":
			if !boundedVideoInteger(value, 1, 120) {
				return false
			}
		case "seed":
			if !boundedVideoInteger(value, -1, math.MaxUint32) {
				return false
			}
		case "ratio", "resolution":
			text, ok := value.(string)
			if !ok || strings.TrimSpace(text) == "" || strings.TrimSpace(text) != text || len(text) > 32 {
				return false
			}
		case "generate_audio", "watermark", "camera_fixed":
			if _, ok := value.(bool); !ok {
				return false
			}
		case "image_url":
			text, ok := value.(string)
			if !ok || len(text) > 2048 {
				return false
			}
			u, err := url.Parse(text)
			if err != nil || u.Host == "" || (u.Scheme != "https" && u.Scheme != "http") || u.User != nil || u.Fragment != "" {
				return false
			}
		default:
			return false
		}
	}
	return true
}

var ValidateVideoModels = validateVideoModels

var VideoModelCapabilities = videoModelCapabilities

var BoundedVideoInteger = boundedVideoInteger

var ValidVideoArguments = validVideoArguments
