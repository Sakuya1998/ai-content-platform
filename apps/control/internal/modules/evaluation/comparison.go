package evaluation

import (
	"math"
	"sort"
)

func CompareReports(baseline, current *EvaluationReport, threshold float64) *EvaluationComparison {
	comparison := &EvaluationComparison{
		Dimensions: map[string]DimensionComparison{},
	}
	if baseline != nil {
		comparison.BaselineID = baseline.ID
	}
	if current != nil {
		comparison.CurrentID = current.ID
	}
	if baseline == nil || current == nil {
		comparison.Status = ComparisonSimilar
		return comparison
	}
	threshold = math.Abs(threshold)
	comparison.OverallDelta = roundScore(current.OverallScore - baseline.OverallScore)
	comparison.Status = classifyDelta(comparison.OverallDelta, threshold)
	for dimension, previous := range baseline.Scores {
		value, exists := current.Scores[dimension]
		if !exists {
			continue
		}
		comparison.Dimensions[dimension] = DimensionComparison{
			Dimension: dimension, Previous: previous, Current: value, Delta: roundScore(value - previous),
		}
	}
	return comparison
}

func CalculateTrend(reports []*EvaluationReport) *EvaluationTrend {
	trend := &EvaluationTrend{Direction: "stable"}
	ordered := make([]*EvaluationReport, 0, len(reports))
	for _, report := range reports {
		if report != nil {
			ordered = append(ordered, report)
		}
	}
	if len(ordered) == 0 {
		return trend
	}
	sort.SliceStable(ordered, func(i, j int) bool { return ordered[i].CreatedAt.Before(ordered[j].CreatedAt) })
	trend.Count = len(ordered)
	for _, report := range ordered {
		trend.AverageScore += report.OverallScore
	}
	trend.AverageScore = roundScore(trend.AverageScore / float64(trend.Count))
	trend.FirstScore = ordered[0].OverallScore
	trend.LatestScore = ordered[len(ordered)-1].OverallScore
	trend.Delta = roundScore(trend.LatestScore - trend.FirstScore)
	switch {
	case trend.Delta > 0:
		trend.Direction = "improving"
	case trend.Delta < 0:
		trend.Direction = "declining"
	}
	return trend
}

func DetectRegression(comparison *EvaluationComparison, threshold float64) []string {
	if comparison == nil {
		return nil
	}
	threshold = math.Abs(threshold)
	regressions := []string{}
	if comparison.OverallDelta < 0 && comparison.OverallDelta <= -threshold {
		regressions = append(regressions, "overall")
	}
	dimensions := make([]string, 0, len(comparison.Dimensions))
	for dimension := range comparison.Dimensions {
		dimensions = append(dimensions, dimension)
	}
	sort.Strings(dimensions)
	for _, dimension := range dimensions {
		if comparison.Dimensions[dimension].Delta < 0 && comparison.Dimensions[dimension].Delta <= -threshold {
			regressions = append(regressions, dimension)
		}
	}
	return regressions
}

func classifyDelta(delta, threshold float64) string {
	if delta == 0 {
		return ComparisonSimilar
	}
	if delta >= threshold {
		return ComparisonImproved
	}
	if delta <= -threshold {
		return ComparisonRegressed
	}
	return ComparisonSimilar
}

func roundScore(value float64) float64 { return math.Round(value*10) / 10 }
