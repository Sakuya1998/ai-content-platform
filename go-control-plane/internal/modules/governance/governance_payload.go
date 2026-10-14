package governance

import (
	"encoding/json"
	"errors"
	"strings"
)

func boundedGovernanceText(value any, limit int) string {
	text, _ := value.(string)
	text = strings.Join(strings.Fields(text), " ")
	runes := []rune(text)
	if len(runes) > limit {
		return string(runes[:limit])
	}
	return text
}

func boundedGovernanceList(value any, limit, textLimit int) []string {
	items, _ := value.([]any)
	result := []string{}
	seen := map[string]bool{}
	for _, item := range items {
		text := boundedGovernanceText(item, textLimit)
		key := strings.ToLower(text)
		if text != "" && !seen[key] {
			result = append(result, text)
			seen[key] = true
			if len(result) >= limit {
				break
			}
		}
	}
	return result
}

func NormalizePayload(kind string, payload json.RawMessage) (json.RawMessage, error) {
	if err := validateResourcePayload(kind, payload); err != nil {
		return nil, err
	}
	var source map[string]any
	_ = json.Unmarshal(payload, &source)
	result := map[string]any{}
	switch kind {
	case "content_types":
		result["parent_id"] = nil
		if id := boundedGovernanceText(source["parent_id"], 120); id != "" {
			result["parent_id"] = id
		}
		result["aliases"] = boundedGovernanceList(source["aliases"], 12, 80)
		result["tags"] = boundedGovernanceList(source["tags"], 20, 50)
	case "styles":
		fields := map[string]bool{"syntax": true, "vocabulary": true, "narrative_techniques": true, "examples": true, "sentence_length": false, "pacing": false}
		for key := range source {
			if _, ok := fields[key]; !ok {
				return nil, errors.New("style contains unsupported field")
			}
		}
		nonempty := false
		for field, list := range fields {
			if list {
				values := boundedGovernanceList(source[field], 12, 300)
				result[field] = values
				nonempty = nonempty || len(values) > 0
			} else {
				text := boundedGovernanceText(source[field], 500)
				result[field] = text
				nonempty = nonempty || text != ""
			}
		}
		if !nonempty {
			return nil, errors.New("style requires at least one description")
		}
	case "quality_policies":
		for key := range source {
			if key != "gate_threshold" && key != "regression_threshold" {
				return nil, errors.New("quality policy contains unsupported field")
			}
		}
		for key, fallback := range map[string]float64{"gate_threshold": 70, "regression_threshold": 3} {
			value := fallback
			if raw, exists := source[key]; exists {
				var ok bool
				value, ok = raw.(float64)
				if !ok || value < 0 || value > 100 {
					return nil, errors.New("quality thresholds must be between 0 and 100")
				}
			}
			result[key] = value
		}
		result["dimensions"] = []string{"plot_coherence", "character_arc", "theme_payoff", "style_consistency", "ending_satisfaction", "unresolved_promises"}
	case "creative_templates":
		result["schema_version"] = "creative-brief-v1"
		result["target_audience"] = boundedGovernanceText(source["target_audience"], 200)
		if result["target_audience"] == "" {
			result["target_audience"] = "General fiction readers"
		}
		defaults := map[string]string{"age_rating": "teen", "point_of_view": "third_limited", "narrative_tense": "past", "narrative_distance": "medium", "ending_tone": "unspecified"}
		choices := map[string]string{"age_rating": "all_ages teen mature", "point_of_view": "first_person third_limited third_omniscient multiple", "narrative_tense": "past present mixed", "narrative_distance": "close medium distant", "ending_tone": "unspecified hopeful bittersweet tragic open"}
		for field, fallback := range defaults {
			value := strings.ToLower(boundedGovernanceText(source[field], 100))
			if !strings.Contains(" "+choices[field]+" ", " "+value+" ") || value == "" {
				value = fallback
			}
			result[field] = value
		}
		for field, limit := range map[string]int{"themes": 8, "must_include": 12, "avoid_content": 12} {
			result[field] = boundedGovernanceList(source[field], limit, 200)
		}
		intensity, _ := source["intensity"].(map[string]any)
		scores := map[string]int{}
		for _, key := range []string{"romance", "mystery", "action", "darkness"} {
			score := 2
			if value, ok := intensity[key].(float64); ok {
				score = int(value)
			}
			if score < 0 {
				score = 0
			}
			if score > 5 {
				score = 5
			}
			scores[key] = score
		}
		result["intensity"] = scores
		result["notes"] = boundedGovernanceText(source["notes"], 2000)
	default:
		return payload, nil
	}
	data, err := json.Marshal(result)
	return data, err
}

var BoundedGovernanceText = boundedGovernanceText

var BoundedGovernanceList = boundedGovernanceList
