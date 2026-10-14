package governance

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

type promptValidationError struct{ message string }

func (e *promptValidationError) Error() string { return e.message }

func promptInvalid(format string, args ...any) error {
	return &promptValidationError{fmt.Sprintf(format, args...)}
}

var novelPromptNodes = []string{"world_builder", "character_designer", "plot_planner", "scene_planner", "scene_writer", "scene_rewriter", "consistency_checker", "style_editor", "quality_evaluator", "replanner", "chapter_digest", "book_auditor", "memory.refresh"}

func workflowPromptNodes(workflow string) ([]string, error) {
	switch workflow {
	case "novel.write@1":
		return append([]string{}, novelPromptNodes...), nil
	case "content.generate@1", "content.adapt@1":
		return []string{"content.plan", "content.write", "content.review", "memory.refresh"}, nil
	case "memory.refresh@1":
		return []string{"memory.refresh"}, nil
	case "media.image@1":
		return []string{"media.image.plan", "media.review", "memory.refresh"}, nil
	case "media.speech@1":
		return []string{"media.speech.plan", "media.review", "memory.refresh"}, nil
	case "media.music@1":
		return []string{"media.music.plan", "media.review", "memory.refresh"}, nil
	case "media.sound_effect@1":
		return []string{"media.sound_effect.plan", "media.review", "memory.refresh"}, nil
	case "media.video@1":
		return []string{"media.plan", "media.review", "memory.refresh"}, nil
	case "media.storyboard@1":
		return []string{"media.storyboard.plan", "media.review", "memory.refresh"}, nil
	case "media.timeline@1":
		return []string{"media.timeline.plan", "media.review", "memory.refresh"}, nil
	case "media.compose@1", "media.assemble@1":
		return []string{"memory.refresh"}, nil
	default:
		return nil, promptInvalid("unknown workflow %q", workflow)
	}
}

func promptHash(v PromptVersion) (string, error) {
	raw, err := CanonicalJSON(map[string]any{"messages": v.Messages, "variables_schema": v.VariablesSchema, "output_schema": v.OutputSchema, "renderer_version": v.RendererVersion})
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func shaPromptBytes(raw []byte) string { sum := sha256.Sum256(raw); return hex.EncodeToString(sum[:]) }

func verifyPromptHash(v PromptVersion) error {
	hash, err := promptHash(v)
	if err != nil {
		return err
	}
	if hash != v.ContentHash {
		return promptInvalid("prompt content hash mismatch")
	}
	return nil
}

var promptPlaceholder = regexp.MustCompile(`\{([a-z_][a-z0-9_]*)\}`)

type denyPromptSchemaLoader struct{}

func (denyPromptSchemaLoader) Load(url string) (any, error) {
	return nil, promptInvalid("external JSON Schema references are forbidden")
}

func compilePromptSchema(raw json.RawMessage) (*jsonschema.Schema, error) {
	var obj map[string]any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&obj); err != nil || obj == nil {
		return nil, promptInvalid("JSON Schema must be an object")
	}
	compiler := jsonschema.NewCompiler()
	compiler.UseLoader(denyPromptSchemaLoader{})
	compiler.DefaultDraft(jsonschema.Draft2020)
	if err := compiler.AddResource("urn:prompt:schema", obj); err != nil {
		return nil, promptInvalid("invalid JSON Schema: %v", err)
	}
	schema, err := compiler.Compile("urn:prompt:schema")
	if err != nil {
		return nil, promptInvalid("invalid JSON Schema: %v", err)
	}
	return schema, nil
}

func validatePromptContract(v PromptVersion, variables, output json.RawMessage, renderer string) error {
	if v.RendererVersion != "literal-v1" || v.RendererVersion != renderer {
		return promptInvalid("renderer must match literal-v1 node contract")
	}
	if len(v.Messages) == 0 || len(v.Messages) > 20 {
		return promptInvalid("messages must contain 1 to 20 messages")
	}
	for _, raw := range []json.RawMessage{v.VariablesSchema, v.OutputSchema, variables, output} {
		if _, err := compilePromptSchema(raw); err != nil {
			return err
		}
	}
	for _, pair := range [][2]json.RawMessage{{v.VariablesSchema, variables}, {v.OutputSchema, output}} {
		left, err := CanonicalJSON(pair[0])
		if err != nil {
			return err
		}
		right, err := CanonicalJSON(pair[1])
		if err != nil {
			return err
		}
		if !bytes.Equal(left, right) {
			return promptInvalid("schema must match node contract")
		}
	}
	var contract struct {
		Type                 string                     `json:"type"`
		Properties           map[string]json.RawMessage `json:"properties"`
		Required             []string                   `json:"required"`
		AdditionalProperties *bool                      `json:"additionalProperties"`
	}
	if err := json.Unmarshal(variables, &contract); err != nil {
		return err
	}
	if contract.Type != "object" || contract.AdditionalProperties == nil || *contract.AdditionalProperties {
		return promptInvalid("variables schema must describe a closed object")
	}
	used := map[string]bool{}
	for _, message := range v.Messages {
		if message.Role != "system" && message.Role != "user" && message.Role != "assistant" {
			return promptInvalid("invalid message role")
		}
		if strings.TrimSpace(message.Content) == "" || len([]rune(message.Content)) > 200000 {
			return promptInvalid("message content must contain 1 to 200000 characters")
		}
		for _, match := range promptPlaceholder.FindAllStringSubmatch(message.Content, -1) {
			key := match[1]
			if _, ok := contract.Properties[key]; !ok {
				return promptInvalid("unknown template variable %s", key)
			}
			used[key] = true
		}
	}
	for _, key := range contract.Required {
		if !used[key] {
			return promptInvalid("required variable %s missing from messages", key)
		}
	}
	for key := range contract.Properties {
		if !used[key] {
			return promptInvalid("declared variable %s missing from messages", key)
		}
	}
	_, err := promptHash(v)
	return err
}

func renderPromptPreview(v PromptVersion, variables map[string]any) ([]PromptMessage, error) {
	schema, err := compilePromptSchema(v.VariablesSchema)
	if err != nil {
		return nil, err
	}
	if err = schema.Validate(variables); err != nil {
		return nil, promptInvalid("variables do not match schema: %v", err)
	}
	result := make([]PromptMessage, len(v.Messages))
	for i, message := range v.Messages {
		result[i] = message
		var missing string
		result[i].Content = promptPlaceholder.ReplaceAllStringFunc(message.Content, func(token string) string {
			key := token[1 : len(token)-1]
			value, ok := variables[key]
			if !ok {
				missing = key
				return token
			}
			if text, ok := value.(string); ok {
				return text
			}
			if value == nil {
				return ""
			}
			raw, e := CanonicalJSON(value)
			if e != nil {
				missing = key
				return token
			}
			return string(raw)
		})
		if missing != "" {
			return nil, promptInvalid("missing or invalid variable %s", missing)
		}
	}
	return result, nil
}

var PromptInvalid = promptInvalid
var WorkflowPromptNodes = workflowPromptNodes
var PromptHash = promptHash
var ShaPromptBytes = shaPromptBytes
var VerifyPromptHash = verifyPromptHash
var CompilePromptSchema = compilePromptSchema
var ValidatePromptContract = validatePromptContract
var RenderPromptPreview = renderPromptPreview

type PromptValidationError = promptValidationError
