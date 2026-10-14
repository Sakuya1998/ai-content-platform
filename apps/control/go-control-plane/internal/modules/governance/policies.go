package governance

import (
	"bytes"
	"encoding/json"
	"strings"
)

func policyObject(properties map[string]any) map[string]any {
	required := make([]string, 0, len(properties))
	for key := range properties {
		required = append(required, key)
	}
	return map[string]any{"type": "object", "properties": properties, "required": required, "additionalProperties": false}
}

func policyNumber(kind string, min, max float64) map[string]any {
	return map[string]any{"type": kind, "minimum": min, "maximum": max}
}

var creationCheckerParameters = map[string]map[string]any{
	"text.length@1":      {"scale": policyNumber("number", 0, 1000)},
	"text.structure@1":   {"min_paragraphs": policyNumber("integer", 1, 100), "min_chars": policyNumber("integer", 1, 100000)},
	"text.repetition@1":  {"duplicate_penalty": policyNumber("number", 0, 100), "window_size": policyNumber("integer", 2, 100), "window_penalty": policyNumber("number", 0, 1000), "max_window_penalty": policyNumber("number", 0, 100)},
	"text.consistency@1": {"high": policyNumber("number", 0, 100), "medium": policyNumber("number", 0, 100), "low": policyNumber("number", 0, 100)},
	"novel.scenes@1":     {"duplicate_penalty": policyNumber("number", 0, 100)},
	"novel.narrative@1":  {"invalid_penalty": policyNumber("number", 0, 100), "duplicate_penalty": policyNumber("number", 0, 100)},
}

func creationPolicySchema() map[string]any {
	stringsList := map[string]any{"type": "array", "minItems": 1, "uniqueItems": true, "items": map[string]any{"type": "string", "minLength": 1, "maxLength": 80}}
	return policyObject(map[string]any{
		"schema_version": map[string]any{"const": "creation-policy@1"},
		"canon":          policyObject(map[string]any{"entity_types": stringsList, "fact_types": stringsList, "require_evidence": map[string]any{"type": "boolean"}, "max_audit_entries": policyNumber("integer", 1, 10000)}),
		"memory":         policyObject(map[string]any{"group_size": policyNumber("integer", 1, 100), "leaf_summary_chars": policyNumber("integer", 1, 20000), "group_summary_chars": policyNumber("integer", 1, 100000), "document_summary_chars": policyNumber("integer", 1, 200000), "neighbor_window": policyNumber("integer", 0, 100)}),
		"constraints":    policyObject(map[string]any{"schema": map[string]any{"type": "object"}, "defaults": map[string]any{"type": "object"}, "labels": map[string]any{"type": "object", "additionalProperties": map[string]any{"type": "string", "minLength": 1}}, "max_prompt_chars": policyNumber("integer", 100, 50000)}),
		"evaluation":     policyObject(map[string]any{"engine_version": map[string]any{"const": "checks@1"}, "gate_threshold": policyNumber("number", 0, 100), "critical_threshold": policyNumber("number", 0, 100), "rules": map[string]any{"type": "array", "minItems": 1, "maxItems": 32, "items": policyObject(map[string]any{"key": map[string]any{"type": "string"}, "dimension": map[string]any{"type": "string", "minLength": 1}, "weight": map[string]any{"type": "number", "exclusiveMinimum": 0}, "critical": map[string]any{"type": "boolean"}, "parameters": map[string]any{"type": "object"}})}}),
	})
}

func validateCreationSchemaRefs(value any) error {
	switch v := value.(type) {
	case map[string]any:
		for key, child := range v {
			if key == "$id" {
				return promptInvalid("creation constraint schema must not declare $id")
			}
			if key == "$ref" || key == "$dynamicRef" {
				target, ok := child.(string)
				if !ok || !strings.HasPrefix(target, "#") {
					return promptInvalid("creation constraint schema references must be local")
				}
			}
			if err := validateCreationSchemaRefs(child); err != nil {
				return err
			}
		}
	case []any:
		for _, child := range v {
			if err := validateCreationSchemaRefs(child); err != nil {
				return err
			}
		}
	}
	return nil
}

func validatePolicyIntegers(object map[string]any, keys ...string) error {
	for _, key := range keys {
		number, ok := object[key].(json.Number)
		if !ok || strings.ContainsAny(number.String(), ".eE") {
			return promptInvalid("creation policy field %s must be an integer JSON value", key)
		}
	}
	return nil
}

func validateCreationPolicy(raw json.RawMessage, workflow string) error {
	var payload map[string]any
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	if err := decoder.Decode(&payload); err != nil || payload == nil {
		return promptInvalid("creation policy payload must be an object")
	}
	schemaRaw, _ := json.Marshal(creationPolicySchema())
	schema, err := compilePromptSchema(schemaRaw)
	if err != nil {
		return err
	}
	if err = schema.Validate(payload); err != nil {
		return promptInvalid("invalid creation policy: %v", err)
	}
	canon := payload["canon"].(map[string]any)
	if err = validatePolicyIntegers(canon, "max_audit_entries"); err != nil {
		return err
	}
	for _, field := range []string{"entity_types", "fact_types"} {
		for _, value := range canon[field].([]any) {
			if strings.TrimSpace(value.(string)) == "" {
				return promptInvalid("creation canon types must be nonempty")
			}
		}
	}
	if err = validatePolicyIntegers(payload["memory"].(map[string]any), "group_size", "leaf_summary_chars", "group_summary_chars", "document_summary_chars", "neighbor_window"); err != nil {
		return err
	}
	if workflow != "" {
		if _, err = workflowPromptNodes(workflow); err != nil {
			return err
		}
	}
	constraints := payload["constraints"].(map[string]any)
	if err = validatePolicyIntegers(constraints, "max_prompt_chars"); err != nil {
		return err
	}
	contract := constraints["schema"].(map[string]any)
	if contract["type"] != "object" || contract["additionalProperties"] != false {
		return promptInvalid("creation constraint schema must describe a closed object")
	}
	if err = validateCreationSchemaRefs(contract); err != nil {
		return err
	}
	contractRaw, _ := json.Marshal(contract)
	validator, err := compilePromptSchema(contractRaw)
	if err != nil {
		return err
	}
	if err = validator.Validate(constraints["defaults"]); err != nil {
		return promptInvalid("creation constraint defaults do not match schema: %v", err)
	}
	properties, _ := contract["properties"].(map[string]any)
	for key, value := range constraints["labels"].(map[string]any) {
		if strings.TrimSpace(value.(string)) == "" {
			return promptInvalid("creation constraint labels must be nonempty")
		}
		if _, ok := properties[key]; !ok {
			return promptInvalid("creation constraint label references unknown field %s", key)
		}
	}
	dimensions := map[string]bool{}
	for _, item := range payload["evaluation"].(map[string]any)["rules"].([]any) {
		rule := item.(map[string]any)
		key := rule["key"].(string)
		dimension := rule["dimension"].(string)
		if strings.TrimSpace(dimension) == "" || len([]rune(dimension)) > 80 || dimensions[dimension] {
			return promptInvalid("creation evaluation dimensions must be nonempty and unique")
		}
		dimensions[dimension] = true
		fields, ok := creationCheckerParameters[key]
		if !ok {
			return promptInvalid("unsupported creation checker %s", key)
		}
		if workflow != "" && workflow != "novel.write@1" && strings.HasPrefix(key, "novel.") {
			return promptInvalid("creation checker %s is not supported by workflow %s", key, workflow)
		}
		parameterSchema := map[string]any{"type": "object", "properties": fields, "additionalProperties": false}
		parameterRaw, _ := json.Marshal(parameterSchema)
		parameterValidator, e := compilePromptSchema(parameterRaw)
		if e != nil {
			return e
		}
		if e = parameterValidator.Validate(rule["parameters"]); e != nil {
			return promptInvalid("invalid parameters for %s: %v", key, e)
		}
		for name := range rule["parameters"].(map[string]any) {
			if fields[name].(map[string]any)["type"] == "integer" {
				if e = validatePolicyIntegers(rule["parameters"].(map[string]any), name); e != nil {
					return e
				}
			}
		}
	}
	_, err = CanonicalJSON(payload)
	return err
}

func creationPolicyHash(raw json.RawMessage) (string, error) {
	value, err := CanonicalJSON(raw)
	if err != nil {
		return "", err
	}
	return shaPromptBytes(value), nil
}

func verifyCreationPolicy(v CreationPolicyVersion, workflow string) error {
	if err := validateCreationPolicy(v.Payload, workflow); err != nil {
		return err
	}
	hash, err := creationPolicyHash(v.Payload)
	if err == nil && hash != v.ContentHash {
		return promptInvalid("creation policy content hash mismatch")
	}
	return err
}

var PolicyObject = policyObject
var PolicyNumber = policyNumber
var CreationPolicySchema = creationPolicySchema
var ValidateCreationSchemaRefs = validateCreationSchemaRefs
var ValidatePolicyIntegers = validatePolicyIntegers
var ValidateCreationPolicy = validateCreationPolicy
var CreationPolicyHash = creationPolicyHash
var VerifyCreationPolicy = verifyCreationPolicy
