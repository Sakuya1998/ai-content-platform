package resources

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"

	executionmodule "github.com/sakuya/novel-agent/apps/control/internal/modules/execution"
)

type ContentEnvelope = executionmodule.Envelope

func (a *ResourceAPI) recordsHTTP(w http.ResponseWriter, r *http.Request, scope, id, kind string, chapter int, artifact string, tail []string) {
	if len(tail) > 0 || r.Method != "GET" {
		methodNotAllowed(w)
		return
	}
	values, err := a.records.Records(r.Context(), scope, id, kind, chapter, artifact)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 200, values)
}

func (a *ResourceAPI) versionHTTP(w http.ResponseWriter, r *http.Request, scope string, novel ProjectResource, tail []string) {
	if len(tail) < 3 {
		http.NotFound(w, r)
		return
	}
	kind, artifact, chapter := "content", "", 0
	if tail[0] == "blocks" {
		var err error
		chapter, err = strconv.Atoi(tail[1])
		if err != nil || chapter < 1 || chapter > 50 {
			a.resourceError(w, errResourceInvalid)
			return
		}
	} else {
		kind = "planning"
		artifact = tail[1]
		if artifact != "blueprint" && artifact != "scene" {
			a.resourceError(w, errResourceInvalid)
			return
		}
		chapter, _ = strconv.Atoi(r.URL.Query().Get("block_index"))
		if artifact == "blueprint" {
			chapter = 0
		} else if chapter < 1 || chapter > 50 {
			a.resourceError(w, errResourceInvalid)
			return
		}
	}
	switch tail[2] {
	case "candidates":
		if len(tail) == 3 {
			if r.Method == "POST" {
				var args map[string]any
				if !readResourceBody(w, r, &args) {
					return
				}
				if args == nil {
					args = map[string]any{}
				}
				args["block_index"] = chapter
				a.operationHTTP(w, r, scope, novel, "candidates", args)
			} else {
				a.recordsHTTP(w, r, scope, novel.ID, "candidate", chapter, "", nil)
			}
			return
		}
		if len(tail) == 5 && tail[4] == "select" && r.Method == "POST" {
			a.operationHTTP(w, r, scope, novel, "resume", map[string]any{"candidate_id": tail[3], "block_index": chapter, "feedback": "approve"})
			return
		}
	case "evaluations":
		a.evaluationHTTP(w, r, scope, novel, chapter, tail[3:])
		return
	case "versions":
		if len(tail) == 3 {
			a.recordsHTTP(w, r, scope, novel.ID, kind, chapter, artifact, nil)
			return
		}
		if tail[3] == "diff" && len(tail) == 4 && r.Method == "GET" {
			a.diffHTTP(w, r, scope, novel.ID, kind, chapter, artifact)
			return
		}
		version, err := strconv.Atoi(tail[3])
		if err != nil || version < 1 {
			a.resourceError(w, errResourceInvalid)
			return
		}
		row, err := a.records.Version(r.Context(), scope, novel.ID, kind, chapter, version, artifact)
		if err != nil {
			a.resourceError(w, err)
			return
		}
		if len(tail) == 4 && r.Method == "GET" {
			writeJSON(w, 200, row)
			return
		}
		if len(tail) == 5 && tail[4] == "restore" && r.Method == "POST" {
			args := map[string]any{"block_index": chapter, "version_number": version, "feedback": "restore", "artifact_type": artifact}
			if kind == "planning" {
				if artifact == "blueprint" {
					args["review_type"] = "blueprint_review"
				} else {
					args["review_type"] = "scene_review"
				}
			}
			a.operationHTTP(w, r, scope, novel, "resume", args)
			return
		}
		if len(tail) == 5 && tail[4] == "evaluate" && r.Method == "POST" {
			var args map[string]any
			if !readResourceBody(w, r, &args) {
				return
			}
			if args == nil {
				args = map[string]any{}
			}
			args["block_index"] = chapter
			args["version_number"] = version
			a.operationHTTP(w, r, scope, novel, "evaluation", args)
			return
		}
	}
	http.NotFound(w, r)
}
func resourceVersionText(row map[string]any) string {
	if text, ok := row["content"].(string); ok {
		return text
	}
	if payload, ok := row["payload"]; ok {
		body, _ := json.MarshalIndent(payload, "", "  ")
		return string(body)
	}
	body, _ := json.MarshalIndent(row, "", "  ")
	return string(body)
}

// A full-context unified diff avoids quadratic storage for large manuscript lines.
func resourceUnifiedDiff(before, after string, from, to int) string {
	if before == after {
		return ""
	}
	a := strings.Split(strings.TrimSuffix(before, "\n"), "\n")
	b := strings.Split(strings.TrimSuffix(after, "\n"), "\n")
	if before == "" {
		a = nil
	}
	if after == "" {
		b = nil
	}
	var out strings.Builder
	fmt.Fprintf(&out, "--- v%d\n+++ v%d\n@@ -1,%d +1,%d @@\n", from, to, len(a), len(b))
	for _, line := range a {
		out.WriteString("-" + line + "\n")
	}
	for _, line := range b {
		out.WriteString("+" + line + "\n")
	}
	return out.String()
}
func (a *ResourceAPI) diffHTTP(w http.ResponseWriter, r *http.Request, scope, id, kind string, chapter int, artifact string) {
	from, e1 := strconv.Atoi(r.URL.Query().Get("from_version"))
	to, e2 := strconv.Atoi(r.URL.Query().Get("to_version"))
	if e1 != nil || e2 != nil || from < 1 || to < 1 {
		a.resourceError(w, errResourceInvalid)
		return
	}
	before, after, err := a.records.Diff(r.Context(), scope, id, kind, chapter, from, to, artifact)
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"from_version": from, "to_version": to, "block_index": chapter, "artifact_type": artifact, "diff": resourceUnifiedDiff(resourceVersionText(before), resourceVersionText(after), from, to)})
}
func (a *ResourceAPI) evaluationHTTP(w http.ResponseWriter, r *http.Request, scope string, novel ProjectResource, chapter int, tail []string) {
	if len(tail) == 0 {
		a.recordsHTTP(w, r, scope, novel.ID, "evaluation", chapter, "", nil)
		return
	}
	if len(tail) == 2 && tail[1] == "baseline" && r.Method == "PUT" {
		err := a.records.Baseline(r.Context(), scope, novel.ID, chapter, tail[0])
		if err != nil {
			a.resourceError(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"baseline_id": tail[0]})
		return
	}
	if len(tail) == 1 && tail[0] == "compare" && r.Method == "GET" {
		records, err := a.records.Records(r.Context(), scope, novel.ID, "evaluation", chapter, "")
		if err != nil {
			a.resourceError(w, err)
			return
		}
		var before, after map[string]any
		baseline := r.URL.Query().Get("baseline_id")
		target := r.URL.Query().Get("target_id")
		if target == "" {
			target = r.URL.Query().Get("evaluation_id")
		}
		from, _ := strconv.Atoi(r.URL.Query().Get("from_version"))
		to, _ := strconv.Atoi(r.URL.Query().Get("to_version"))
		threshold := 3.0
		if raw := r.URL.Query().Get("regression_threshold"); raw != "" {
			var err error
			threshold, err = strconv.ParseFloat(raw, 64)
			if err != nil || math.IsNaN(threshold) || math.IsInf(threshold, 0) || threshold < 0 || threshold > 100 {
				a.resourceError(w, errResourceInvalid)
				return
			}
		}
		for _, record := range records {
			key := resourceText(record["record_key"])
			if key == baseline || record["record_id"] == baseline || (baseline == "" && record["baseline"] == true && (from == 0 || resourceEvaluationVersion(record) == from)) || (from > 0 && resourceEvaluationVersion(record) == from && before == nil) {
				before = record
			}
			if key == target || record["record_id"] == target || (target == "" && after == nil && (to == 0 || resourceEvaluationVersion(record) == to)) {
				after = record
			}
		}
		if before == nil || after == nil {
			a.resourceError(w, ErrNotFound)
			return
		}
		result := compareResourceEvaluations(before, after, threshold)
		result["baseline"] = before
		result["target"] = after
		result["score_delta"] = result["overall_delta"]
		writeJSON(w, 200, result)
		return
	}
	http.NotFound(w, r)
}
func resourceEvaluationVersion(record map[string]any) int {
	if version := resourceInt(record["content_version"]); version > 0 {
		return version
	}
	return resourceInt(record["version_number"])
}
func compareResourceEvaluations(before, after map[string]any, threshold float64) map[string]any {
	threshold = math.Abs(threshold)
	delta := math.Round((resourceNumericScore(after)-resourceNumericScore(before))*10) / 10
	status := "stable"
	if delta <= -threshold {
		status = "regressed"
	} else if delta >= threshold {
		status = "improved"
	}
	old, newScores := map[string]any{}, map[string]any{}
	for _, field := range []string{"deterministic_scores", "judge_scores"} {
		if scores, ok := before[field].(map[string]any); ok {
			for k, v := range scores {
				old[k] = v
			}
		}
		if scores, ok := after[field].(map[string]any); ok {
			for k, v := range scores {
				newScores[k] = v
			}
		}
	}
	dimensions := map[string]any{}
	for field, value := range old {
		from, ok1 := value.(float64)
		to, ok2 := newScores[field].(float64)
		if ok1 && ok2 {
			dimensions[field] = map[string]any{"from": from, "to": to, "delta": math.Round((to-from)*10) / 10}
		}
	}
	return map[string]any{"from_evaluation_id": before["id"], "to_evaluation_id": after["id"], "from_version": resourceEvaluationVersion(before), "to_version": resourceEvaluationVersion(after), "overall_delta": delta, "status": status, "regression_threshold": threshold, "dimensions": dimensions}
}
func resourceNumericScore(row map[string]any) float64 {
	for _, key := range []string{"overall_score", "score", "total_score"} {
		if value, ok := row[key].(float64); ok {
			return value
		}
	}
	if scores, ok := row["scores"].(map[string]any); ok {
		return resourceNumericScore(scores)
	}
	return 0
}
func (a *ResourceAPI) conflictsHTTP(w http.ResponseWriter, r *http.Request, scope string, novel ProjectResource) {
	if r.Method != "GET" {
		methodNotAllowed(w)
		return
	}
	chapter := 0
	if value := r.URL.Query().Get("block_index"); value != "" {
		chapter, _ = strconv.Atoi(value)
		if chapter < 1 || chapter > 50 {
			a.resourceError(w, errResourceInvalid)
			return
		}
	}
	if conflicts, ok := novel.State["conflicts"]; ok {
		writeJSON(w, 200, conflicts)
		return
	}
	values, err := a.records.Records(r.Context(), scope, novel.ID, "conflict", chapter, "")
	if err != nil {
		a.resourceError(w, err)
		return
	}
	if len(values) == 0 {
		if report, ok := novel.State["quality_report"]; ok {
			writeJSON(w, 200, map[string]any{"block_index": chapter, "issues": novel.State["consistency_issues"], "report": report})
			return
		}
		if issues, ok := novel.State["issues"]; ok {
			writeJSON(w, 200, map[string]any{"block_index": chapter, "issues": issues, "report": novel.State["quality_report"]})
			return
		}
	}
	writeJSON(w, 200, map[string]any{"block_index": chapter, "issues": values})
}
func (a *ResourceAPI) memoryHTTP(w http.ResponseWriter, r *http.Request, scope string, novel ProjectResource) {
	values, err := a.records.Records(r.Context(), scope, novel.ID, "memory", 0, "")
	if err != nil {
		a.resourceError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"canon": novel.State["canon"], "snapshots": values, "block_summaries": novel.State["block_summaries"], "act_summaries": novel.State["act_summaries"], "content_summary": novel.State["content_summary"]})
}
func (a *ResourceAPI) usageHTTP(w http.ResponseWriter, r *http.Request, scope, id string) {
	if r.Method != "GET" {
		methodNotAllowed(w)
		return
	}
	records, err := a.records.Records(r.Context(), scope, id, "usage", 0, "")
	if err != nil {
		a.resourceError(w, err)
		return
	}
	totals := map[string]float64{"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost": 0}
	for _, row := range records {
		for key := range totals {
			if value, ok := row[key].(float64); ok {
				totals[key] += value
			}
		}
	}
	writeJSON(w, 200, map[string]any{"totals": totals, "calls": records})
}

// Stable chapter order is also used by text exporters and imported backups.
func sortResourceChapters(values []map[string]any) {
	sort.SliceStable(values, func(i, j int) bool {
		return resourceInt(values[i]["block_index"]) < resourceInt(values[j]["block_index"])
	})
}
