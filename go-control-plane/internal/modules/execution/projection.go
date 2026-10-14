// Package execution defines the read projection emitted by workflow adapters.
package execution

type Envelope struct {
	State            map[string]any   `json:"state"`
	ContentVersions  []map[string]any `json:"content_versions"`
	PlanningVersions []map[string]any `json:"planning_versions"`
	Candidates       []map[string]any `json:"candidates"`
	Evaluations      []map[string]any `json:"evaluations"`
	Audits           []map[string]any `json:"audits"`
	MemorySnapshots  []map[string]any `json:"memory_snapshots"`
	MemoryQuality    []map[string]any `json:"memory_quality"`
	Usage            []map[string]any `json:"usage"`
	Traces           []map[string]any `json:"traces"`
}

func (e Envelope) Groups() map[string][]map[string]any {
	return map[string][]map[string]any{"content": e.ContentVersions, "planning": e.PlanningVersions, "candidate": e.Candidates, "evaluation": e.Evaluations, "audit": e.Audits, "memory": e.MemorySnapshots, "memory_quality": e.MemoryQuality, "usage": e.Usage, "trace": e.Traces}
}
