package postgres

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"math"
	"strings"
	"time"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/evaluation"
	"github.com/jackc/pgx/v5"
)

type benchmarkRecord struct {
	report        *evaluation.EvaluationReport
	policyVersion string
	overallScore  *float64
}

func benchmarkMetadata(id string, raw json.RawMessage) (benchmarkRecord, error) {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var source map[string]any
	if err := decoder.Decode(&source); err != nil {
		return benchmarkRecord{}, err
	}
	if source == nil {
		return benchmarkRecord{}, evaluation.ErrInvalid
	}
	report := &evaluation.EvaluationReport{ID: id, Scores: map[string]float64{}}
	report.SchemaVersion, _ = source["schema_version"].(string)
	if report.SchemaVersion == "" {
		report.SchemaVersion, _ = source["evaluator_version"].(string)
	}
	if version, ok := source["policy_version"].(string); ok {
		report.PolicyVersion = version
	}
	score, hasOverallScore := numeric(source["overall_score"])
	if hasOverallScore {
		report.OverallScore = score
	}
	if scores, ok := numericMap(source["scores"]); ok {
		mergeScores(report.Scores, scores)
	}
	if scores, ok := numericMap(source["deterministic_scores"]); ok {
		mergeScores(report.Scores, scores)
	}
	if scores, ok := numericMap(source["judge_scores"]); ok {
		mergeScores(report.Scores, scores)
	}
	if cases, ok := source["cases"].([]any); ok {
		scoreTotals := map[string]float64{}
		scoreCounts := map[string]int{}
		for _, rawCase := range cases {
			item, ok := rawCase.(map[string]any)
			if !ok {
				continue
			}
			for _, field := range []string{"deterministic_scores", "judge_scores"} {
				if scores, ok := numericMap(item[field]); ok {
					for dimension, score := range scores {
						scoreTotals[dimension] += score
						scoreCounts[dimension]++
					}
				}
			}
			appendFindings(report, item["findings"])
		}
		for dimension, total := range scoreTotals {
			if _, exists := report.Scores[dimension]; !exists {
				report.Scores[dimension] = round(total / float64(scoreCounts[dimension]))
			}
		}
	}
	appendFindings(report, source["findings"])
	if !hasOverallScore && len(report.Scores) > 0 {
		for _, score := range report.Scores {
			report.OverallScore += score
		}
		report.OverallScore = round(report.OverallScore / float64(len(report.Scores)))
	}
	var overall *float64
	if hasOverallScore || len(report.Scores) > 0 {
		value := report.OverallScore
		overall = &value
	}
	return benchmarkRecord{report: report, policyVersion: report.PolicyVersion, overallScore: overall}, nil
}

func appendFindings(report *evaluation.EvaluationReport, raw any) {
	items, ok := raw.([]any)
	if !ok {
		return
	}
	for _, value := range items {
		item, ok := value.(map[string]any)
		if !ok {
			continue
		}
		finding := evaluation.Finding{}
		finding.Dimension, _ = item["dimension"].(string)
		finding.Message, _ = item["message"].(string)
		finding.Source, _ = item["source"].(string)
		finding.EvaluatorKey, _ = item["evaluator_key"].(string)
		if score, ok := numeric(item["score"]); ok {
			finding.Score = &score
		}
		report.Findings = append(report.Findings, finding)
	}
}

func numericMap(raw any) (map[string]float64, bool) {
	values, ok := raw.(map[string]any)
	if !ok {
		return nil, false
	}
	result := make(map[string]float64, len(values))
	for key, rawValue := range values {
		if value, ok := numeric(rawValue); ok {
			result[key] = value
		}
	}
	return result, true
}

func numeric(raw any) (float64, bool) {
	var value float64
	switch typed := raw.(type) {
	case json.Number:
		parsed, err := typed.Float64()
		if err != nil {
			return 0, false
		}
		value = parsed
	case float64:
		value = typed
	case int:
		value = float64(typed)
	default:
		return 0, false
	}
	return value, !math.IsNaN(value) && !math.IsInf(value, 0)
}

func mergeScores(target, source map[string]float64) {
	for dimension, score := range source {
		target[dimension] = score
	}
}

func nullableString(value string) any {
	if strings.TrimSpace(value) == "" {
		return nil
	}
	return value
}

func nullableScore(value *float64) any {
	if value == nil {
		return nil
	}
	return *value
}

func (r *Repository) BenchmarkReport(ctx context.Context, workspace, id string) (*evaluation.EvaluationReport, error) {
	var report *evaluation.EvaluationReport
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		var raw json.RawMessage
		var jobID, policyVersion *string
		var createdAt time.Time
		if err := tx.QueryRow(ctx, `SELECT report,job_id,policy_version,created_at FROM evaluation_benchmark_runs
			WHERE id::text=$1 AND workspace_id=$2`, id, workspace).Scan(&raw, &jobID, &policyVersion, &createdAt); err != nil {
			return err
		}
		record, err := benchmarkMetadata(id, raw)
		if err != nil {
			return err
		}
		report = record.report
		report.WorkspaceID = workspace
		if jobID != nil {
			report.JobID = *jobID
		}
		if policyVersion != nil {
			report.PolicyVersion = *policyVersion
		}
		report.CreatedAt = createdAt
		return nil
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, evaluation.ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	return report, nil
}

func (r *Repository) GetBenchmark(ctx context.Context, workspace, id string) (*evaluation.EvaluationReport, error) {
	return r.BenchmarkReport(ctx, workspace, id)
}

func (r *Repository) GetJobBenchmarks(ctx context.Context, workspace, jobID string) ([]*evaluation.EvaluationReport, error) {
	reports := []*evaluation.EvaluationReport{}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `SELECT id::text,report,coalesce(job_id,id::text),policy_version,created_at
			FROM evaluation_benchmark_runs WHERE workspace_id=$1 AND job_id=$2
			ORDER BY created_at DESC,id`, workspace, jobID)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var id, job string
			var raw json.RawMessage
			var policyVersion *string
			var createdAt time.Time
			if err = rows.Scan(&id, &raw, &job, &policyVersion, &createdAt); err != nil {
				return err
			}
			metadata, parseErr := benchmarkMetadata(id, raw)
			if parseErr != nil {
				return parseErr
			}
			metadata.report.WorkspaceID = workspace
			metadata.report.JobID = job
			metadata.report.CreatedAt = createdAt
			if policyVersion != nil {
				metadata.report.PolicyVersion = *policyVersion
			}
			reports = append(reports, metadata.report)
		}
		return rows.Err()
	})
	return reports, err
}

func (r *Repository) CompareBenchmarks(ctx context.Context, workspace, baselineID, currentID string, threshold float64) (*evaluation.EvaluationComparison, error) {
	baseline, err := r.BenchmarkReport(ctx, workspace, baselineID)
	if err != nil {
		return nil, err
	}
	current, err := r.BenchmarkReport(ctx, workspace, currentID)
	if err != nil {
		return nil, err
	}
	return evaluation.CompareReports(baseline, current, threshold), nil
}

func round(value float64) float64 { return math.Round(value*10) / 10 }
