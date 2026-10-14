package objectstorage

import (
	"encoding/json"
	"os"
	"testing"
)

func TestSharedObjectKeyContract(t *testing.T) {
	data, err := os.ReadFile("../../../../contracts/object-keys.examples.json")
	if err != nil {
		t.Fatal(err)
	}
	var examples []struct {
		WorkspaceID string `json:"workspace_id"`
		ProjectID   string `json:"project_id"`
		Category    string `json:"category"`
		ResourceID  string `json:"resource_id"`
		VersionID   string `json:"version_id"`
		Filename    string `json:"filename"`
		Expected    string `json:"expected"`
	}
	if err := json.Unmarshal(data, &examples); err != nil {
		t.Fatal(err)
	}
	for _, example := range examples {
		key, err := ObjectKey(example.WorkspaceID, example.ProjectID, example.Category, example.ResourceID, example.Filename, example.VersionID)
		if err != nil || key != example.Expected {
			t.Fatalf("object key = %q, error = %v", key, err)
		}
	}
}

func TestObjectKeyRejectsTraversalAndMissingVersion(t *testing.T) {
	for _, invalid := range []string{"", "..", "a/b", `a\b`} {
		if _, err := ObjectKey("ws-1", invalid, "assets", "asset-1", "video.mp4", "v-1"); err == nil {
			t.Fatalf("invalid project ID accepted: %q", invalid)
		}
	}
	if _, err := ObjectKey("ws-1", "project-1", "documents", "doc-1", "content.json", ""); err == nil {
		t.Fatal("versioned object without version ID accepted")
	}
	if _, err := ObjectKey("ws-1", "project-1", "exports", "export-1", "video.mp4", "v-1"); err == nil {
		t.Fatal("export with document version accepted")
	}
}
