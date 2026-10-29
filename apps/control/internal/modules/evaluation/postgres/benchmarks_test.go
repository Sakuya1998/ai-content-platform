package postgres

import (
	"encoding/json"
	"testing"
)

func TestBenchmarkMetadataReadsRuntimeEvaluationAndSuiteReports(t *testing.T) {
	tests := []struct {
		name          string
		report        string
		wantScore     float64
		wantPolicy    string
		wantDimension float64
	}{
		{
			name:      "evaluation report",
			report:    `{"schema_version":"evaluation@1","overall_score":82.5,"policy_version":"content-quality@1","scores":{"length":80,"structure":85}}`,
			wantScore: 82.5, wantPolicy: "content-quality@1", wantDimension: 80,
		},
		{
			name:      "benchmark suite",
			report:    `{"overall_score":84,"cases":[{"deterministic_scores":{"quality":80},"judge_scores":{"quality":90}},{"deterministic_scores":{"quality":70}}]}`,
			wantScore: 84, wantDimension: 80,
		},
		{
			name:      "legacy dimensions without top level score",
			report:    `{"scores":{"length":80,"structure":90}}`,
			wantScore: 85, wantDimension: 80,
		},
		{
			name:      "explicit zero score is preserved",
			report:    `{"overall_score":0,"scores":{"length":80}}`,
			wantScore: 0, wantDimension: 80,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			var raw json.RawMessage = []byte(test.report)
			metadata, err := benchmarkMetadata("run-1", raw)
			if err != nil {
				t.Fatal(err)
			}
			if metadata.overallScore == nil || *metadata.overallScore != test.wantScore || metadata.policyVersion != test.wantPolicy {
				t.Fatalf("metadata score/policy = %v/%q", metadata.overallScore, metadata.policyVersion)
			}
			if metadata.report.Scores["quality"] != test.wantDimension && metadata.report.Scores["length"] != test.wantDimension {
				t.Fatalf("normalized dimensions = %#v", metadata.report.Scores)
			}
		})
	}
}
