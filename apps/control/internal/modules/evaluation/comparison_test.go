package evaluation

import (
	"testing"
	"time"
)

func TestCompareReportsClassifiesDeltaAndOnlyComparesSharedDimensions(t *testing.T) {
	baseline := &EvaluationReport{ID: "before", OverallScore: 80, Scores: map[string]float64{"length": 70, "structure": 90}}
	current := &EvaluationReport{ID: "after", OverallScore: 84, Scores: map[string]float64{"length": 76, "style": 88}}

	comparison := CompareReports(baseline, current, 3)
	if comparison.Status != ComparisonImproved || comparison.OverallDelta != 4 {
		t.Fatalf("comparison status/delta = %q/%.1f", comparison.Status, comparison.OverallDelta)
	}
	if len(comparison.Dimensions) != 1 || comparison.Dimensions["length"].Delta != 6 {
		t.Fatalf("dimension comparisons = %#v", comparison.Dimensions)
	}
}

func TestCompareReportsMarksThresholdBoundaryAsRegressed(t *testing.T) {
	comparison := CompareReports(
		&EvaluationReport{OverallScore: 80, Scores: map[string]float64{}},
		&EvaluationReport{OverallScore: 77, Scores: map[string]float64{}},
		3,
	)
	if comparison.Status != ComparisonRegressed {
		t.Fatalf("status = %q, want regressed", comparison.Status)
	}
}

func TestCompareReportsZeroThresholdKeepsUnchangedReportsSimilar(t *testing.T) {
	comparison := CompareReports(
		&EvaluationReport{OverallScore: 80, Scores: map[string]float64{"length": 75}},
		&EvaluationReport{OverallScore: 80, Scores: map[string]float64{"length": 75}},
		0,
	)
	if comparison.Status != ComparisonSimilar || len(DetectRegression(comparison, 0)) != 0 {
		t.Fatalf("zero-threshold comparison = %#v", comparison)
	}
}

func TestCalculateTrendOrdersScoresAndDetectsDimensionRegression(t *testing.T) {
	reports := []*EvaluationReport{
		{CreatedAt: timeAt(3), OverallScore: 70, Scores: map[string]float64{"length": 65}},
		{CreatedAt: timeAt(1), OverallScore: 60, Scores: map[string]float64{"length": 75}},
		{CreatedAt: timeAt(2), OverallScore: 65, Scores: map[string]float64{"length": 68}},
	}
	trend := CalculateTrend(reports)
	if trend.Count != 3 || trend.FirstScore != 60 || trend.LatestScore != 70 || trend.Direction != "improving" {
		t.Fatalf("trend = %#v", trend)
	}
	comparison := CompareReports(reports[1], reports[0], 4)
	regressions := DetectRegression(comparison, 4)
	if len(regressions) != 1 || regressions[0] != "length" {
		t.Fatalf("dimension regression was not detected: %v", regressions)
	}
	comparison = CompareReports(reports[0], reports[1], 4)
	regressions = DetectRegression(comparison, 4)
	if len(regressions) != 1 || regressions[0] != "overall" {
		t.Fatalf("overall regression was not detected: %v", regressions)
	}
}

func timeAt(day int) time.Time { return time.Date(2026, time.January, day, 0, 0, 0, 0, time.UTC) }
