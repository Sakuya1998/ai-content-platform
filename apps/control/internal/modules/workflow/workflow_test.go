package workflow

import (
	"testing"

	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/governance"
)

func TestWorkflowDefinitionRejectsUnregisteredEngineAndCode(t *testing.T) {
	nodes, _ := governance.WorkflowPromptNodes("content.generate@1")
	if err := Validate(Definition{Template: "content.generate@1", Nodes: nodes}); err != nil {
		t.Fatal(err)
	}
	for _, definition := range []Definition{{Template: "python.exec@1", Nodes: []string{"eval"}}, {Template: "content.generate@1", Nodes: []string{"custom.code"}}} {
		if err := Validate(definition); err == nil {
			t.Fatal("uncontrolled execution graph accepted")
		}
	}
}
