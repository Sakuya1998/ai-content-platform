package execution

import (
	"github.com/google/uuid"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/content"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/governance"
	"github.com/sakuya/novel-agent/apps/control/internal/modules/production"
	"strings"
)

type GenerationRequest = Request

var validComposeArguments = content.ValidComposeArguments
var validStoryboardArguments = content.ValidStoryboardArguments
var validTimelineArguments = content.ValidTimelineArguments
var validAssembleArguments = production.ValidAssembleArguments
var validVideoArguments = governance.ValidVideoArguments
var validImageArguments = governance.ValidImageArguments
var validAudioGenerationArguments = governance.ValidAudioGenerationArguments

func ValidGeneration(request GenerationRequest) bool {
	if len(request.IdempotencyKey) > 200 {
		return false
	}
	if _, err := uuid.Parse(request.ProjectID); err != nil {
		return false
	}
	if request.DocumentID != "" {
		if _, err := uuid.Parse(request.DocumentID); err != nil {
			return false
		}
	}
	if request.SourceVersionID != "" {
		if _, err := uuid.Parse(request.SourceVersionID); err != nil {
			return false
		}
	}
	if len(request.InputVersionIDs) > 256 {
		return false
	}
	if len(request.InputVersionIDs) > 0 && request.DocumentType != "storyboard" && request.DocumentType != "timeline" && !(request.DocumentType == "video" && request.Operation == "compose") {
		return false
	}
	seenInputs := map[uuid.UUID]bool{}
	for _, id := range request.InputVersionIDs {
		version, err := uuid.Parse(id)
		if err != nil || seenInputs[version] {
			return false
		}
		seenInputs[version] = true
	}
	if strings.TrimSpace(request.Objective) == "" || len(request.Objective) > 16000 || len(strings.TrimSpace(request.Title)) == 0 || len(request.Title) > 200 {
		return false
	}
	switch request.DocumentType {
	case "novel", "screenplay", "copy", "short_drama", "advertisement", "generic", "video", "image", "audio", "storyboard", "timeline":
	default:
		return false
	}
	if request.Operation != "" && !(request.DocumentType == "video" && request.Operation == "compose") && !(request.DocumentType == "timeline" && request.Operation == "assemble") {
		return false
	}
	if request.Operation == "assemble" {
		if request.SourceVersionID == "" || !validAssembleArguments(request.Arguments) || len(request.Nodes) > 0 && strings.Join(request.Nodes, ",") != "validate,assemble,publish" {
			return false
		}
	} else if request.Operation == "compose" {
		if request.SourceVersionID == "" || !validComposeArguments(request.Arguments) || len(request.Nodes) > 0 && strings.Join(request.Nodes, ",") != "validate,render,publish" {
			return false
		}
	} else if request.DocumentType == "storyboard" || request.DocumentType == "timeline" {
		validArgs := validStoryboardArguments(request.Arguments)
		if request.DocumentType == "timeline" {
			validArgs = validTimelineArguments(request.Arguments)
		}
		if !validArgs || len(request.Nodes) > 0 && strings.Join(request.Nodes, ",") != "plan,review" {
			return false
		}
	} else if request.DocumentType == "video" || request.DocumentType == "image" || request.DocumentType == "audio" {
		validArguments := validVideoArguments(request.Arguments)
		if request.DocumentType == "image" {
			validArguments = validImageArguments(request.Arguments, request.SourceVersionID)
		}
		if request.DocumentType == "audio" {
			_, validArguments = validAudioGenerationArguments(request.Arguments)
		}
		if len(request.Nodes) > 0 && strings.Join(request.Nodes, ",") != "plan,generate,review" || !validArguments {
			return false
		}
	} else if len(request.Nodes) > 0 && strings.Join(request.Nodes, ",") != "plan,write,review" && strings.Join(request.Nodes, ",") != "write,review" {
		return false
	}
	if len(request.RequiredMemoryVersions) > 100 {
		return false
	}
	for _, version := range request.RequiredMemoryVersions {
		if _, err := uuid.Parse(version); err != nil {
			return false
		}
	}
	return true
}
