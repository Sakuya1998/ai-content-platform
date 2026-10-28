package evaluation

import (
	"errors"
	"math"
	"strings"
	"time"
)

const (
	PolicyStatusDraft     = "draft"
	PolicyStatusPublished = "published"
	PolicyStatusArchived  = "archived"

	ComparisonImproved  = "improved"
	ComparisonRegressed = "regressed"
	ComparisonSimilar   = "similar"
)

var (
	ErrInvalid     = errors.New("invalid evaluation request")
	ErrNotFound    = errors.New("evaluation resource not found")
	ErrConflict    = errors.New("evaluation resource conflict")
	ErrUnavailable = errors.New("evaluation store unavailable")
)

type PolicyFilters struct {
	PolicyID string
	Status   string
}

type BenchmarkFilters struct {
	JobID  string
	Status string
}

type EvaluationPolicyVersion struct {
	PolicyID          string           `json:"policy_id"`
	Version           int              `json:"version"`
	EngineVersion     string           `json:"engine_version"`
	GateThreshold     float64          `json:"gate_threshold"`
	CriticalThreshold float64          `json:"critical_threshold"`
	Rules             []EvaluationRule `json:"rules"`
	Status            string           `json:"status"`
	PublishedAt       *time.Time       `json:"published_at,omitempty"`
	CreatedAt         time.Time        `json:"created_at,omitempty"`
}

type EvaluationRule struct {
	Key        string         `json:"key"`
	Dimension  string         `json:"dimension"`
	Weight     float64        `json:"weight"`
	Critical   bool           `json:"critical"`
	Parameters map[string]any `json:"parameters"`
}

func (p EvaluationPolicyVersion) Validate() error {
	if strings.TrimSpace(p.PolicyID) == "" || p.Version < 1 || strings.TrimSpace(p.EngineVersion) == "" {
		return errors.New("evaluation policy identity and engine version are required")
	}
	if !validScore(p.GateThreshold) || !validScore(p.CriticalThreshold) {
		return errors.New("evaluation thresholds must be between 0 and 100")
	}
	if p.Status != "" && p.Status != PolicyStatusDraft && p.Status != PolicyStatusPublished && p.Status != PolicyStatusArchived {
		return errors.New("evaluation policy status is invalid")
	}
	if len(p.Rules) == 0 {
		return errors.New("evaluation policy requires at least one rule")
	}
	if len(p.Rules) > 32 {
		return errors.New("evaluation policy cannot contain more than 32 rules")
	}
	keys := make(map[string]struct{}, len(p.Rules))
	dimensions := make(map[string]struct{}, len(p.Rules))
	for _, rule := range p.Rules {
		key := strings.TrimSpace(rule.Key)
		dimension := strings.TrimSpace(rule.Dimension)
		if key == "" || len(key) > 120 || dimension == "" || len(dimension) > 80 ||
			math.IsNaN(rule.Weight) || math.IsInf(rule.Weight, 0) || rule.Weight <= 0 || len(rule.Parameters) > 64 {
			return errors.New("evaluation rules require a key, dimension, and positive finite weight")
		}
		if _, exists := keys[key]; exists {
			return errors.New("evaluation rule keys must be unique")
		}
		if _, exists := dimensions[dimension]; exists {
			return errors.New("evaluation rule dimensions must be unique")
		}
		keys[key] = struct{}{}
		dimensions[dimension] = struct{}{}
	}
	return nil
}

func validScore(score float64) bool {
	return !math.IsNaN(score) && !math.IsInf(score, 0) && score >= 0 && score <= 100
}

type EvaluationReport struct {
	ID            string             `json:"id"`
	WorkspaceID   string             `json:"workspace_id,omitempty"`
	JobID         string             `json:"job_id,omitempty"`
	PolicyVersion string             `json:"policy_version,omitempty"`
	SchemaVersion string             `json:"schema_version,omitempty"`
	Scores        map[string]float64 `json:"scores,omitempty"`
	OverallScore  float64            `json:"overall_score"`
	Findings      []Finding          `json:"findings,omitempty"`
	CreatedAt     time.Time          `json:"created_at,omitempty"`
}

type Finding struct {
	Dimension    string   `json:"dimension"`
	Score        *float64 `json:"score,omitempty"`
	Message      string   `json:"message"`
	Source       string   `json:"source,omitempty"`
	EvaluatorKey string   `json:"evaluator_key,omitempty"`
}

type DimensionDefinition struct {
	Key         string    `json:"key"`
	DisplayName string    `json:"display_name"`
	Category    string    `json:"category"`
	Description string    `json:"description,omitempty"`
	CheckKeys   []string  `json:"check_keys"`
	IsBuiltin   bool      `json:"is_builtin"`
	CreatedAt   time.Time `json:"created_at,omitempty"`
}

type EvaluationComparison struct {
	BaselineID   string                         `json:"baseline_id"`
	CurrentID    string                         `json:"current_id"`
	Status       string                         `json:"status"`
	OverallDelta float64                        `json:"overall_delta"`
	Dimensions   map[string]DimensionComparison `json:"dimensions"`
}

type DimensionComparison struct {
	Dimension string  `json:"dimension"`
	Delta     float64 `json:"delta"`
	Previous  float64 `json:"previous"`
	Current   float64 `json:"current"`
}

type EvaluationTrend struct {
	Count        int     `json:"count"`
	AverageScore float64 `json:"average_score"`
	FirstScore   float64 `json:"first_score"`
	LatestScore  float64 `json:"latest_score"`
	Delta        float64 `json:"delta"`
	Direction    string  `json:"direction"`
}

type WorkspaceStats struct {
	TotalReports  int     `json:"total_reports"`
	ScoredReports int     `json:"scored_reports"`
	AverageScore  float64 `json:"average_score"`
	PassedReports int     `json:"passed_reports"`
	FailedReports int     `json:"failed_reports"`
	PassRate      float64 `json:"pass_rate"`
}

type TrendPoint struct {
	CreatedAt time.Time `json:"created_at"`
	Score     float64   `json:"score"`
	Count     int       `json:"count"`
}

type IssueFrequency struct {
	Dimension string `json:"dimension"`
	Message   string `json:"message"`
	Count     int    `json:"count"`
}
