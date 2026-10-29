package postgres

import (
	"context"
	"encoding/json"
	"sort"
	"strings"
	"time"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/evaluation"
	"github.com/jackc/pgx/v5"
)

type reportSample struct {
	report    json.RawMessage
	createdAt time.Time
	score     *float64
	status    string
}

func (r *Repository) GetWorkspaceStats(ctx context.Context, workspace string, since time.Time) (*evaluation.WorkspaceStats, error) {
	stats := &evaluation.WorkspaceStats{}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		var sinceFilter any
		if !since.IsZero() {
			sinceFilter = since
		}
		return tx.QueryRow(ctx, `WITH reports AS (
			SELECT
				CASE WHEN overall_score IS NOT NULL THEN overall_score::double precision
					WHEN jsonb_typeof(report->'overall_score')='number' THEN (report->>'overall_score')::double precision
					ELSE NULL END AS score,
				CASE WHEN jsonb_typeof(report->'passed')='boolean' THEN report->>'passed'='true'
					ELSE report->>'status'='passed' END AS passed,
				CASE WHEN jsonb_typeof(report->'passed')='boolean' THEN report->>'passed'='false'
					ELSE report->>'status' IN ('failed','revise') END AS failed
			FROM evaluation_benchmark_runs
			WHERE workspace_id=$1 AND ($2::timestamptz IS NULL OR created_at >= $2)
		)
		SELECT count(*)::integer,count(score)::integer,coalesce(avg(score),0)::double precision,
			count(*) FILTER (WHERE passed)::integer,count(*) FILTER (WHERE failed)::integer
		FROM reports`, workspace, sinceFilter).Scan(
			&stats.TotalReports, &stats.ScoredReports, &stats.AverageScore, &stats.PassedReports, &stats.FailedReports,
		)
	})
	stats.AverageScore = round(stats.AverageScore)
	if stats.PassedReports+stats.FailedReports > 0 {
		stats.PassRate = round(float64(stats.PassedReports) * 100 / float64(stats.PassedReports+stats.FailedReports))
	}
	return stats, err
}

func (r *Repository) GetDimensionTrends(ctx context.Context, workspace, dimension string, limit int) ([]*evaluation.TrendPoint, error) {
	points := []*evaluation.TrendPoint{}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `SELECT report,created_at,overall_score,status FROM evaluation_benchmark_runs
			WHERE workspace_id=$1 AND status='completed' AND (
				(jsonb_typeof(report->'scores')='object' AND (report->'scores') ? $2) OR
				(jsonb_typeof(report->'deterministic_scores')='object' AND (report->'deterministic_scores') ? $2) OR
				(jsonb_typeof(report->'judge_scores')='object' AND (report->'judge_scores') ? $2) OR
				EXISTS (
					SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(report->'cases')='array'
						THEN report->'cases' ELSE '[]'::jsonb END) case_report
					WHERE (jsonb_typeof(case_report->'deterministic_scores')='object' AND case_report->'deterministic_scores' ? $2)
						OR (jsonb_typeof(case_report->'judge_scores')='object' AND case_report->'judge_scores' ? $2)
				)
			) ORDER BY created_at DESC,id LIMIT $3`, workspace, dimension, limit)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var sample reportSample
			if err = rows.Scan(&sample.report, &sample.createdAt, &sample.score, &sample.status); err != nil {
				return err
			}
			metadata, parseErr := benchmarkMetadata("", sample.report)
			if parseErr != nil {
				return parseErr
			}
			if score, ok := metadata.report.Scores[dimension]; ok {
				points = append(points, &evaluation.TrendPoint{CreatedAt: sample.createdAt, Score: score, Count: 1})
			}
		}
		return rows.Err()
	})
	return points, err
}

func (r *Repository) GetTopIssues(ctx context.Context, workspace string, limit int) ([]*evaluation.IssueFrequency, error) {
	issues := []*evaluation.IssueFrequency{}
	err := r.within(ctx, workspace, func(tx pgx.Tx) error {
		rows, err := tx.Query(ctx, `WITH findings AS (
			SELECT finding->>'dimension' AS dimension,finding->>'message' AS message
			FROM evaluation_benchmark_runs run
			CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(run.report->'findings')='array'
				THEN run.report->'findings' ELSE '[]'::jsonb END) finding
			WHERE run.workspace_id=$1 AND run.status='completed'
			UNION ALL
			SELECT finding->>'dimension' AS dimension,finding->>'message' AS message
			FROM evaluation_benchmark_runs run
			CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(run.report->'cases')='array'
				THEN run.report->'cases' ELSE '[]'::jsonb END) case_report
			CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(case_report->'findings')='array'
				THEN case_report->'findings' ELSE '[]'::jsonb END) finding
			WHERE run.workspace_id=$1 AND run.status='completed'
		)
		SELECT dimension,message,count(*)::integer FROM findings
		WHERE nullif(btrim(dimension),'') IS NOT NULL AND nullif(btrim(message),'') IS NOT NULL
		GROUP BY dimension,message ORDER BY count(*) DESC,dimension,message LIMIT $2`, workspace, limit)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			item := &evaluation.IssueFrequency{}
			if err = rows.Scan(&item.Dimension, &item.Message, &item.Count); err != nil {
				return err
			}
			issues = append(issues, item)
		}
		return rows.Err()
	})
	return issues, err
}

func (r *Repository) ListDimensions(ctx context.Context, category string) ([]*evaluation.DimensionDefinition, error) {
	if r.pool == nil {
		return nil, evaluation.ErrUnavailable
	}
	rows, err := r.pool.Query(ctx, `SELECT key,display_name,category,coalesce(description,''),check_keys,is_builtin,created_at
		FROM evaluation_dimensions WHERE ($1='' OR category=$1) ORDER BY category,key`, category)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	items := []*evaluation.DimensionDefinition{}
	for rows.Next() {
		var item evaluation.DimensionDefinition
		var checkKeys []byte
		if err = rows.Scan(&item.Key, &item.DisplayName, &item.Category, &item.Description, &checkKeys, &item.IsBuiltin, &item.CreatedAt); err != nil {
			return nil, err
		}
		if err = json.Unmarshal(checkKeys, &item.CheckKeys); err != nil {
			return nil, err
		}
		items = append(items, &item)
	}
	return items, rows.Err()
}

func (r *Repository) RegisterDimension(ctx context.Context, dimension *evaluation.DimensionDefinition) error {
	if r.pool == nil {
		return evaluation.ErrUnavailable
	}
	if dimension == nil || dimension.Key == "" || dimension.DisplayName == "" || dimension.IsBuiltin || len(dimension.CheckKeys) == 0 {
		return evaluation.ErrInvalid
	}
	checkKeys, err := json.Marshal(dimension.CheckKeys)
	if err != nil {
		return err
	}
	_, err = r.pool.Exec(ctx, `INSERT INTO evaluation_dimensions(key,display_name,category,description,check_keys,is_builtin)
		VALUES($1,$2,$3,NULLIF($4,''),$5,false)`, dimension.Key, dimension.DisplayName, dimension.Category, dimension.Description, checkKeys)
	return policyError(err)
}

func aggregateWorkspaceStats(rows []reportSample) evaluation.WorkspaceStats {
	stats := evaluation.WorkspaceStats{TotalReports: len(rows)}
	totalScore := 0.0
	for _, row := range rows {
		var report map[string]any
		_ = json.Unmarshal(row.report, &report)
		if row.score != nil {
			stats.ScoredReports++
			totalScore += *row.score
		}
		passed := report["status"] == "passed"
		failed := report["status"] == "failed" || report["status"] == "revise"
		if explicit, ok := report["passed"].(bool); ok {
			passed = explicit
			failed = !explicit
		}
		if passed {
			stats.PassedReports++
		}
		if failed {
			stats.FailedReports++
		}
	}
	if stats.ScoredReports > 0 {
		stats.AverageScore = round(totalScore / float64(stats.ScoredReports))
	}
	finished := stats.PassedReports + stats.FailedReports
	if finished > 0 {
		stats.PassRate = round(float64(stats.PassedReports) * 100 / float64(finished))
	}
	return stats
}

func dimensionTrend(rows []reportSample, dimension string, limit int) []*evaluation.TrendPoint {
	points := []*evaluation.TrendPoint{}
	for _, row := range rows {
		metadata, err := benchmarkMetadata("", row.report)
		if err != nil {
			continue
		}
		if score, ok := metadata.report.Scores[dimension]; ok {
			points = append(points, &evaluation.TrendPoint{CreatedAt: row.createdAt, Score: score, Count: 1})
		}
	}
	sort.Slice(points, func(i, j int) bool { return points[i].CreatedAt.After(points[j].CreatedAt) })
	if limit >= 0 && len(points) > limit {
		points = points[:limit]
	}
	return points
}

func topIssues(rows []reportSample, limit int) []*evaluation.IssueFrequency {
	counts := map[string]*evaluation.IssueFrequency{}
	issues := []*evaluation.IssueFrequency{}
	for _, row := range rows {
		var report map[string]any
		if json.Unmarshal(row.report, &report) != nil {
			continue
		}
		countFindings := func(raw any) {
			findings, ok := raw.([]any)
			if !ok {
				return
			}
			for _, rawFinding := range findings {
				finding, ok := rawFinding.(map[string]any)
				if !ok {
					continue
				}
				dimension, _ := finding["dimension"].(string)
				message, _ := finding["message"].(string)
				dimension, message = strings.TrimSpace(dimension), strings.TrimSpace(message)
				if dimension == "" || message == "" {
					continue
				}
				key := dimension + "\x00" + message
				if counts[key] == nil {
					counts[key] = &evaluation.IssueFrequency{Dimension: dimension, Message: message}
				}
				counts[key].Count++
			}
		}
		countFindings(report["findings"])
		if cases, ok := report["cases"].([]any); ok {
			for _, rawCase := range cases {
				if item, ok := rawCase.(map[string]any); ok {
					countFindings(item["findings"])
				}
			}
		}
	}
	for _, issue := range counts {
		issues = append(issues, issue)
	}
	sort.Slice(issues, func(i, j int) bool {
		if issues[i].Count != issues[j].Count {
			return issues[i].Count > issues[j].Count
		}
		if issues[i].Dimension != issues[j].Dimension {
			return issues[i].Dimension < issues[j].Dimension
		}
		return issues[i].Message < issues[j].Message
	})
	if limit >= 0 && len(issues) > limit {
		issues = issues[:limit]
	}
	return issues
}
