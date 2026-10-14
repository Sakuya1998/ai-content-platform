package task

import (
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/project"
)

func ResourceSnapshot(resource project.Resource, args map[string]any, workflow string) map[string]any {
	skill, pack := governance.WorkflowCapabilityDefaults(workflow)
	return map[string]any{"schema_version": "1.0", "workflow": workflow, "skills": []string{skill}, "prompt_packs": []string{pack}, "tools": []string{}, "mcp_policies": []string{}, "model_routes": map[string]string{"creative": "runtime-creative@1", "analysis": "runtime-analysis@1", "embedding": "runtime-embedding@1"}, "schema_versions": map[string]string{"content_ir": "1.0"}}
}
