package governance

import (
	"errors"
)

var musicOutputFormats = []string{"mp3_44100_128", "mp3_48000_192"}

var soundEffectOutputFormats = []string{
	"mp3_22050_32", "mp3_24000_48", "mp3_44100_32", "mp3_44100_64", "mp3_44100_96", "mp3_44100_128", "mp3_44100_192",
}

func validateMusicModels(models []MusicModel) error {
	if len(models) > 50 {
		return errors.New("too many music models")
	}
	names := map[string]bool{}
	for _, model := range models {
		name := model.ModelName
		capabilities := model.Capabilities
		if !validMediaString(name, 200) || names[name] || !modelContains([]string{"music_v1", "music_v2", "music_v2_5"}, name) {
			return errors.New("invalid or duplicate music model name")
		}
		names[name] = true
		if !validMediaList(capabilities.OutputFormats, 2, 32, musicOutputFormats) || capabilities.MinDurationMS < 3000 || capabilities.MinDurationMS > 600000 || capabilities.MaxDurationMS < capabilities.MinDurationMS || capabilities.MaxDurationMS > 600000 {
			return errors.New("invalid music model capabilities")
		}
	}
	return nil
}

func validateSoundEffectModels(models []SoundEffectModel) error {
	if len(models) > 50 {
		return errors.New("too many sound effect models")
	}
	names := map[string]bool{}
	for _, model := range models {
		name := model.ModelName
		capabilities := model.Capabilities
		if !validMediaString(name, 200) || names[name] || name != "eleven_text_to_sound_v2" {
			return errors.New("invalid or duplicate sound effect model name")
		}
		names[name] = true
		if !validMediaList(capabilities.OutputFormats, 7, 32, soundEffectOutputFormats) ||
			!boundedMediaReal(capabilities.MinDurationSeconds, 0.5, 30) ||
			!boundedMediaReal(capabilities.MaxDurationSeconds, capabilities.MinDurationSeconds, 30) {
			return errors.New("invalid sound effect model capabilities")
		}
	}
	return nil
}

func musicModelCapabilities(models []MusicModel, name string) (MusicCapabilities, bool) {
	for _, model := range models {
		if model.ModelName == name {
			return model.Capabilities, true
		}
	}
	return MusicCapabilities{}, false
}

func soundEffectModelCapabilities(models []SoundEffectModel, name string) (SoundEffectCapabilities, bool) {
	for _, model := range models {
		if model.ModelName == name {
			return model.Capabilities, true
		}
	}
	return SoundEffectCapabilities{}, false
}

func validMusicArguments(arguments map[string]any) bool {
	parameters, ok := mediaArguments(arguments, "music")
	if !ok {
		return false
	}
	for key, value := range parameters {
		switch key {
		case "output_format":
			text, ok := value.(string)
			if !ok || !modelContains(musicOutputFormats, text) {
				return false
			}
		case "music_length_ms":
			if !boundedVideoInteger(value, 3000, 600000) {
				return false
			}
		case "force_instrumental":
			if _, ok := value.(bool); !ok {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func validSoundEffectArguments(arguments map[string]any) bool {
	parameters, ok := mediaArguments(arguments, "effect")
	if !ok {
		return false
	}
	for key, value := range parameters {
		switch key {
		case "output_format":
			text, ok := value.(string)
			if !ok || !modelContains(soundEffectOutputFormats, text) {
				return false
			}
		case "duration_seconds":
			if !boundedMediaReal(value, 0.5, 30) {
				return false
			}
		case "prompt_influence":
			if !boundedMediaReal(value, 0, 1) {
				return false
			}
		case "loop":
			if _, ok := value.(bool); !ok {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func validAudioGenerationArguments(arguments map[string]any) (string, bool) {
	if len(arguments) == 0 {
		return "speech_synthesis", true
	}
	if len(arguments) != 1 {
		return "", false
	}
	if _, ok := arguments["audio"]; ok {
		return "speech_synthesis", validSpeechArguments(arguments)
	}
	if _, ok := arguments["music"]; ok {
		return "music_generation", validMusicArguments(arguments)
	}
	if _, ok := arguments["effect"]; ok {
		return "sound_effect_generation", validSoundEffectArguments(arguments)
	}
	return "", false
}

var ValidateMusicModels = validateMusicModels

var ValidateSoundEffectModels = validateSoundEffectModels

var MusicModelCapabilities = musicModelCapabilities

var SoundEffectModelCapabilities = soundEffectModelCapabilities

var ValidMusicArguments = validMusicArguments

var ValidSoundEffectArguments = validSoundEffectArguments

var ValidAudioGenerationArguments = validAudioGenerationArguments
