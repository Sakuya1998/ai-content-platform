package task

import "testing"

func TestProjectStatePreservesTerminalAndCancellationOutcomes(t *testing.T) {
	for _, tc := range []struct {
		current, event, want string
		accepted             bool
	}{
		{"queued", "job.accepted", "queued", true},
		{"queued", "job.running", "running", true},
		{"running", "job.waiting_review", "waiting_review", true},
		{"waiting_review", "job.accepted", "queued", true},
		{"running", "job.retry_scheduled", "queued", true},
		{"cancel_requested", "job.running", "cancel_requested", false},
		{"cancel_requested", "job.waiting_review", "cancel_requested", false},
		{"cancel_requested", "job.cancelled", "cancelled", true},
		{"cancelled", "job.completed", "cancelled", false},
		{"failed", "job.running", "failed", false},
		{"completed", "job.failed", "completed", false},
		{"running", "project.state.changed", "running", false},
	} {
		got, accepted := ProjectState(tc.current, tc.event)
		if got != tc.want || accepted != tc.accepted {
			t.Fatalf("%s + %s => %s/%t", tc.current, tc.event, got, accepted)
		}
	}
}
