package task

func Terminal(status string) bool {
	return status == "completed" || status == "failed" || status == "cancelled"
}

// ProjectState folds Runtime events into the public query state.
func ProjectState(current, event string) (status string, accepted bool) {
	status = map[string]string{
		"job.accepted": "queued", "job.running": "running", "job.waiting_review": "waiting_review",
		"job.completed": "completed", "job.failed": "failed", "job.cancel_requested": "cancel_requested",
		"job.cancelled": "cancelled", "job.retry_scheduled": "queued",
	}[event]
	if status == "" || Terminal(current) {
		return current, false
	}
	if current == "cancel_requested" && (status == "queued" || status == "running" || status == "waiting_review") {
		return current, false
	}
	return status, true
}
