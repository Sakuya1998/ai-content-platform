package postgres

import (
	"context"
	"encoding/json"
	"os"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestDimensionTrendAndTopIssuesUseRuntimeReportAndCaseFindings(t *testing.T) {
	rows := []reportSample{
		{
			createdAt: time.Date(2026, 10, 19, 0, 0, 0, 0, time.UTC),
			report:    json.RawMessage(`{"scores":{"length":80},"cases":[{"findings":[{"dimension":"consistency","message":"unsupported claim"}]}]}`),
		},
		{
			createdAt: time.Date(2026, 10, 18, 0, 0, 0, 0, time.UTC),
			report:    json.RawMessage(`{"scores":{"length":70},"findings":[{"dimension":"consistency","message":"unsupported claim"}]}`),
		},
	}
	points := dimensionTrend(rows, "length", 100)
	if len(points) != 2 || points[0].Score != 80 || points[1].Score != 70 {
		t.Fatalf("dimension points = %#v", points)
	}
	issues := topIssues(rows, 5)
	if len(issues) != 1 || issues[0].Dimension != "consistency" || issues[0].Message != "unsupported claim" || issues[0].Count != 2 {
		t.Fatalf("top issues = %#v", issues)
	}
}

func TestDimensionTrendLimitKeepsLatestMatchingReportsWhenDimensionIsSparse(t *testing.T) {
	rows := []reportSample{
		{createdAt: time.Date(2026, 10, 19, 0, 0, 0, 0, time.UTC), report: json.RawMessage(`{"scores":{"other":80}}`)},
		{createdAt: time.Date(2026, 10, 18, 0, 0, 0, 0, time.UTC), report: json.RawMessage(`{"scores":{"length":70}}`)},
	}
	points := dimensionTrend(rows, "length", 1)
	if len(points) != 1 || points[0].Score != 70 {
		t.Fatalf("sparse dimension points = %#v", points)
	}
}

func TestEvaluationPostgresAnalyticsAreWorkspaceScopedAndFilterBeforeLimit(t *testing.T) {
	url := os.Getenv("MQ_TEST_CONTROL_DATABASE_URL")
	if url == "" {
		t.Skip("MQ_TEST_CONTROL_DATABASE_URL is not configured")
	}
	pool, err := pgxpool.New(context.Background(), url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)

	ctx := context.Background()
	workspace := uuid.NewString()
	if _, err = pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Evaluation analytics')", workspace); err != nil {
		t.Fatal(err)
	}
	repository := NewRepository(pool)
	oldID, failedID, recentID := uuid.NewString(), uuid.NewString(), uuid.NewString()
	err = repository.within(ctx, workspace, func(tx pgx.Tx) error {
		if err := RecordBenchmark(ctx, tx, workspace, oldID, json.RawMessage(`{"overall_score":80,"scores":{"length":80},"status":"passed","findings":[{"dimension":"consistency","message":"unsupported claim"}]}`)); err != nil {
			return err
		}
		if err := RecordBenchmark(ctx, tx, workspace, failedID, json.RawMessage(`{"overall_score":60,"status":"failed","cases":[{"findings":[{"dimension":"consistency","message":"unsupported claim"}]}]}`)); err != nil {
			return err
		}
		if err := RecordBenchmark(ctx, tx, workspace, recentID, json.RawMessage(`{"overall_score":90,"scores":{"style":90},"status":"passed"}`)); err != nil {
			return err
		}
		_, err := tx.Exec(ctx, `UPDATE evaluation_benchmark_runs SET created_at=CASE id
			WHEN $1::uuid THEN now()-interval '2 minutes'
			WHEN $2::uuid THEN now()-interval '1 minute'
			ELSE now() END WHERE id IN ($1::uuid,$2::uuid,$3::uuid)`, oldID, failedID, recentID)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}

	stats, err := repository.GetWorkspaceStats(ctx, workspace, time.Time{})
	if err != nil {
		t.Fatalf("workspace stats: %v", err)
	}
	if stats.TotalReports != 3 || stats.ScoredReports != 3 || stats.PassedReports != 2 || stats.FailedReports != 1 || stats.AverageScore != 76.7 || stats.PassRate != 66.7 {
		t.Fatalf("workspace stats = %#v", stats)
	}
	trend, err := repository.GetDimensionTrends(ctx, workspace, "length", 1)
	if err != nil || len(trend) != 1 || trend[0].Score != 80 {
		t.Fatalf("sparse dimension trend = %#v, err=%v", trend, err)
	}
	issues, err := repository.GetTopIssues(ctx, workspace, 5)
	if err != nil || len(issues) != 1 || issues[0].Count != 2 {
		t.Fatalf("top issues = %#v, err=%v", issues, err)
	}
	otherWorkspaceStats, err := repository.GetWorkspaceStats(ctx, uuid.NewString(), time.Time{})
	if err != nil || otherWorkspaceStats.TotalReports != 0 {
		t.Fatalf("cross-workspace stats = %#v, err=%v", otherWorkspaceStats, err)
	}
}
