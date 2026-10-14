package execution

import "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"

func GeneratedAudioWorkflow(arguments map[string]any) string {
	purpose, valid := governance.ValidAudioGenerationArguments(arguments)
	if !valid {
		return "media.speech@1"
	}
	switch purpose {
	case "music_generation":
		return "media.music@1"
	case "sound_effect_generation":
		return "media.sound_effect@1"
	default:
		return "media.speech@1"
	}
}

func AudioWorkflowBinding(workflow string) (string, string) {
	switch workflow {
	case "media.music@1":
		return "controlled-music@1", "music_generation"
	case "media.sound_effect@1":
		return "controlled-sound-effect@1", "sound_effect_generation"
	default:
		return "controlled-speech@1", "speech_synthesis"
	}
}

func DefaultCapabilitySnapshot(request Request, workflow string) map[string]any {
	snapshot := map[string]any{"schema_version": "1.0", "workflow": workflow, "skills": []string{"controlled-text@1"}, "prompt_packs": []string{"content@1"}, "tools": []string{}, "mcp_policies": []string{}, "model_routes": map[string]string{"creative": "runtime-creative@1", "analysis": "runtime-analysis@1", "embedding": "runtime-embedding@1"}, "schema_versions": map[string]string{"content_ir": "1.0"}}
	if request.DocumentType == "video" {
		snapshot["skills"] = []string{"controlled-video@1"}
		snapshot["prompt_packs"] = []string{"media@1"}
		snapshot["model_routes"] = map[string]string{"creative": "runtime-creative@1", "analysis": "runtime-analysis@1", "embedding": "runtime-embedding@1", "video_generation": "runtime-video@1"}
	}
	if request.DocumentType == "image" || request.DocumentType == "audio" {
		skill, purpose := "controlled-image@1", "image_generation"
		if request.DocumentType == "audio" {
			skill, purpose = AudioWorkflowBinding(GeneratedAudioWorkflow(request.Arguments))
		}
		snapshot["skills"] = []string{skill}
		snapshot["prompt_packs"] = []string{"media@1"}
		snapshot["model_routes"].(map[string]string)[purpose] = "current"
	}
	if request.DocumentType == "storyboard" || request.DocumentType == "timeline" || request.Operation == "compose" {
		kind := request.DocumentType
		if request.Operation == "compose" {
			kind = "compose"
		}
		snapshot["skills"] = []string{"controlled-" + kind + "@1"}
		snapshot["prompt_packs"] = []string{"media@1"}
		snapshot["model_routes"] = map[string]string{"analysis": "runtime-analysis@1", "embedding": "runtime-embedding@1"}
		schemas := map[string]string{"content_ir": "1.0"}
		if kind == "compose" {
			schemas["timeline"], schemas["renderer"] = "timeline@1", "ffmpeg@1"
		} else {
			schemas[kind] = kind + "@1"
		}
		snapshot["schema_versions"] = schemas
	}
	if request.Operation == "assemble" {
		snapshot["skills"] = []string{"controlled-assemble@1"}
		snapshot["model_routes"] = map[string]string{"embedding": "runtime-embedding@1"}
		snapshot["schema_versions"] = map[string]string{"content_ir": "1.0", "storyboard": "storyboard@1", "timeline": "timeline@1"}
	}
	return snapshot
}
