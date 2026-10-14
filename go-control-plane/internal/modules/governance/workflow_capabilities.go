package governance

// WorkflowCapabilityDefaults names published catalog requirements, not execution steps.
func WorkflowCapabilityDefaults(workflow string) (string, string) {
	defaults := map[string][2]string{"novel.write@1": {"legacy-novel@1", "novel@1"}, "content.generate@1": {"controlled-text@1", "content@1"}, "content.adapt@1": {"controlled-text@1", "content@1"}}
	if value, ok := defaults[workflow]; ok {
		return value[0], value[1]
	}
	return "controlled-text@1", "content@1"
}
