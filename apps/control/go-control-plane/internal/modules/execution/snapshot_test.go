package execution

import "testing"

func TestDefaultCapabilitySnapshotBuildsVideoContract(t *testing.T) {
	snapshot := DefaultCapabilitySnapshot(Request{DocumentType: "video"}, "media.video@1")
	if snapshot["workflow"] != "media.video@1" {
		t.Fatalf("workflow = %#v", snapshot["workflow"])
	}
	if got := snapshot["skills"].([]string); len(got) != 1 || got[0] != "controlled-video@1" {
		t.Fatalf("skills = %#v", got)
	}
	routes := snapshot["model_routes"].(map[string]string)
	if routes["video_generation"] != "runtime-video@1" || routes["creative"] != "runtime-creative@1" {
		t.Fatalf("model routes = %#v", routes)
	}
}

func TestDefaultCapabilitySnapshotBuildsAssembleContract(t *testing.T) {
	snapshot := DefaultCapabilitySnapshot(Request{DocumentType: "timeline", Operation: "assemble"}, "media.assemble@1")
	if got := snapshot["skills"].([]string); len(got) != 1 || got[0] != "controlled-assemble@1" {
		t.Fatalf("skills = %#v", got)
	}
	routes := snapshot["model_routes"].(map[string]string)
	if len(routes) != 1 || routes["embedding"] != "runtime-embedding@1" {
		t.Fatalf("model routes = %#v", routes)
	}
	schemas := snapshot["schema_versions"].(map[string]string)
	if schemas["storyboard"] != "storyboard@1" || schemas["timeline"] != "timeline@1" {
		t.Fatalf("schema versions = %#v", schemas)
	}
}
