package governancehttp

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/google/uuid"
)

func testCreationPolicy() map[string]any {
	return map[string]any{
		"schema_version": "creation-policy@1",
		"canon":          map[string]any{"entity_types": []string{"person", "place"}, "fact_types": []string{"attribute", "event"}, "require_evidence": true, "max_audit_entries": 200},
		"memory":         map[string]any{"group_size": 5, "leaf_summary_chars": 1500, "group_summary_chars": 3000, "document_summary_chars": 6000, "neighbor_window": 2},
		"constraints":    map[string]any{"schema": map[string]any{"type": "object", "properties": map[string]any{"tone": map[string]any{"type": "string"}}, "additionalProperties": false}, "defaults": map[string]any{"tone": "neutral"}, "labels": map[string]any{"tone": "Tone"}, "max_prompt_chars": 2000},
		"evaluation":     map[string]any{"engine_version": "checks@1", "gate_threshold": 75, "critical_threshold": 60, "rules": []any{map[string]any{"key": "text.length@1", "dimension": "length", "weight": 1, "critical": false, "parameters": map[string]any{"scale": 100}}}},
	}
}
func TestCreationPolicyValidation(t *testing.T) {
	payload := testCreationPolicy()
	raw, _ := json.Marshal(payload)
	for _, workflow := range []string{"novel.write@1", "content.generate@1", "content.adapt@1", "memory.refresh@1", "media.video@1", "media.image@1", "media.speech@1", "media.music@1", "media.sound_effect@1"} {
		if err := validateCreationPolicy(raw, workflow); err != nil {
			t.Fatalf("%s: %v", workflow, err)
		}
	}
	cases := map[string]func(map[string]any){
		"unknown root":         func(p map[string]any) { p["code"] = "eval()" },
		"unknown nested":       func(p map[string]any) { p["memory"].(map[string]any)["arbitrary"] = 1 },
		"unsupported schema":   func(p map[string]any) { p["schema_version"] = "creation-policy@2" },
		"group limit":          func(p map[string]any) { p["memory"].(map[string]any)["group_size"] = 101 },
		"fractional integer":   func(p map[string]any) { p["memory"].(map[string]any)["group_size"] = 1.5 },
		"float integer":        func(p map[string]any) { p["memory"].(map[string]any)["group_size"] = json.Number("1.0") },
		"exponent integer":     func(p map[string]any) { p["memory"].(map[string]any)["group_size"] = json.Number("1e1") },
		"blank canon type":     func(p map[string]any) { p["canon"].(map[string]any)["entity_types"] = []string{" "} },
		"boolean integer":      func(p map[string]any) { p["memory"].(map[string]any)["group_size"] = true },
		"duplicate canon type": func(p map[string]any) { p["canon"].(map[string]any)["entity_types"] = []string{"person", "person"} },
		"unknown checker": func(p map[string]any) {
			p["evaluation"].(map[string]any)["rules"].([]any)[0].(map[string]any)["key"] = "python.eval@1"
		},
		"zero weight": func(p map[string]any) {
			p["evaluation"].(map[string]any)["rules"].([]any)[0].(map[string]any)["weight"] = 0
		},
		"blank dimension": func(p map[string]any) {
			p["evaluation"].(map[string]any)["rules"].([]any)[0].(map[string]any)["dimension"] = " "
		},
		"duplicate dimension": func(p map[string]any) {
			rules := p["evaluation"].(map[string]any)["rules"].([]any)
			p["evaluation"].(map[string]any)["rules"] = append(rules, rules[0])
		},
		"unknown parameter": func(p map[string]any) {
			p["evaluation"].(map[string]any)["rules"].([]any)[0].(map[string]any)["parameters"] = map[string]any{"expression": "1+1"}
		},
		"parameter bound": func(p map[string]any) {
			p["evaluation"].(map[string]any)["rules"].([]any)[0].(map[string]any)["parameters"] = map[string]any{"scale": 1001}
		},
		"schema remote ref": func(p map[string]any) {
			p["constraints"].(map[string]any)["schema"].(map[string]any)["$ref"] = "https://invalid.test/schema"
		},
		"schema id": func(p map[string]any) {
			p["constraints"].(map[string]any)["schema"].(map[string]any)["$id"] = "https://invalid.test/schema"
		},
		"schema open": func(p map[string]any) {
			p["constraints"].(map[string]any)["schema"].(map[string]any)["additionalProperties"] = true
		},
		"invalid default": func(p map[string]any) { p["constraints"].(map[string]any)["defaults"] = map[string]any{"tone": true} },
		"unknown label": func(p map[string]any) {
			p["constraints"].(map[string]any)["labels"] = map[string]any{"hidden": "Hidden"}
		},
		"blank label": func(p map[string]any) {
			p["constraints"].(map[string]any)["labels"] = map[string]any{"tone": " "}
		},
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			candidate := testCreationPolicy()
			change(candidate)
			raw, _ := json.Marshal(candidate)
			if err := validateCreationPolicy(raw, "novel.write@1"); err == nil {
				t.Fatal("invalid policy accepted")
			}
		})
	}
	payload = testCreationPolicy()
	payload["evaluation"].(map[string]any)["rules"].([]any)[0].(map[string]any)["key"] = "novel.scenes@1"
	payload["evaluation"].(map[string]any)["rules"].([]any)[0].(map[string]any)["parameters"] = map[string]any{}
	raw, _ = json.Marshal(payload)
	if err := validateCreationPolicy(raw, "novel.write@1"); err != nil {
		t.Fatal(err)
	}
	for _, workflow := range []string{"content.generate@1", "content.adapt@1", "memory.refresh@1"} {
		if err := validateCreationPolicy(raw, workflow); err == nil {
			t.Fatal("novel-only checker accepted for", workflow)
		}
	}
	payload = testCreationPolicy()
	contract := payload["constraints"].(map[string]any)["schema"].(map[string]any)
	contract["$defs"] = map[string]any{"tone": map[string]any{"type": "string"}}
	contract["properties"] = map[string]any{"tone": map[string]any{"$ref": "#/$defs/tone"}}
	raw, _ = json.Marshal(payload)
	if err := validateCreationPolicy(raw, ""); err != nil {
		t.Fatal("local references rejected:", err)
	}
}
func TestCreationPolicyCanonicalHash(t *testing.T) {
	payload := testCreationPolicy()
	raw, _ := json.Marshal(payload)
	hash, err := creationPolicyHash(raw)
	if err != nil {
		t.Fatal(err)
	}
	spaced := bytes.Buffer{}
	if err = json.Indent(&spaced, raw, "", "  "); err != nil {
		t.Fatal(err)
	}
	other, err := creationPolicyHash(spaced.Bytes())
	if err != nil || hash != other {
		t.Fatal("hash depends on JSON whitespace")
	}
	v := CreationPolicyVersion{Payload: raw, ContentHash: hash}
	if err = verifyCreationPolicy(v, "content.generate@1"); err != nil {
		t.Fatal(err)
	}
	v.ContentHash = "tampered"
	if err = verifyCreationPolicy(v, ""); err == nil {
		t.Fatal("tampered hash accepted")
	}
}
func creationPolicyRequest(api *CreationPolicyAPI, p Principal, method, path string, body any) *httptest.ResponseRecorder {
	raw, _ := json.Marshal(body)
	r := httptest.NewRequest(method, path, bytes.NewReader(raw))
	r = r.WithContext(WithPrincipal(r.Context(), p))
	w := httptest.NewRecorder()
	api.ServeHTTP(w, r)
	return w
}
func TestCreationPolicyAuthenticationAndRBAC(t *testing.T) {
	api := NewCreationPolicyAPI(nil, nil, "internal-test")
	workspace := uuid.NewString()
	for _, path := range []string{"/api/v2/workspaces/" + workspace + "/creation-policies", "/internal/v1/creation-policies/" + uuid.NewString()} {
		w := httptest.NewRecorder()
		api.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		expected := 401
		if strings.HasPrefix(path, "/internal/") {
			expected = 404
		}
		if w.Code != expected {
			t.Fatal("unauthenticated request accepted", w.Code)
		}
	}
	p := Principal{WorkspaceID: workspace, Role: "viewer"}
	w := creationPolicyRequest(api, p, "POST", "/api/v2/workspaces/"+workspace+"/creation-policies", testCreationPolicy())
	if w.Code != 403 {
		t.Fatal("viewer mutation accepted", w.Code)
	}
	p.Role = "owner"
	w = creationPolicyRequest(api, p, "GET", "/api/v2/workspaces/"+uuid.NewString()+"/creation-policies", nil)
	if w.Code != 403 {
		t.Fatal("cross workspace request accepted", w.Code)
	}
}
func TestCreationPolicyPostgresLifecycle(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	workspace := uuid.NewString()
	user := uuid.NewString()
	tx, err := workspaceTx(ctx, pool, workspace)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, `INSERT INTO workspaces(id,name) VALUES($1,'creation policy test')`, workspace); err == nil {
		_, err = tx.Exec(ctx, `INSERT INTO users(id,workspace_id,email,role) VALUES($1,$2,$3,'owner')`, user, workspace, user+"@invalid.test")
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		tx.Rollback(ctx)
		t.Fatal(err)
	}
	api := NewCreationPolicyAPI(pool, nil, "internal-test")
	p := Principal{UserID: user, WorkspaceID: workspace, Role: "owner"}
	prefix := "/api/v2/workspaces/" + workspace
	var baseline map[string]string
	for _, workflow := range []string{"novel.write@1", "content.generate@1", "content.adapt@1", "memory.refresh@1", "media.video@1", "media.image@1", "media.speech@1", "media.music@1", "media.sound_effect@1"} {
		frozen, e := api.Freeze(ctx, workspace, workflow)
		if e != nil {
			t.Fatal(e)
		}
		if workflow == "content.generate@1" {
			baseline = frozen
		}
		v, e := api.FrozenVersion(ctx, workspace, frozen["version_id"])
		if e != nil || v.ContentHash != frozen["content_hash"] {
			t.Fatal("seed hash mismatch", e)
		}
	}
	original, err := api.FrozenVersion(ctx, workspace, baseline["version_id"])
	if err != nil {
		t.Fatal(err)
	}
	w := creationPolicyRequest(api, p, "POST", prefix+"/creation-policies/"+original.ID+"/disable", map[string]any{})
	if w.Code != 409 {
		t.Fatal("platform modified", w.Code, w.Body.String())
	}
	w = creationPolicyRequest(api, p, "POST", prefix+"/creation-policies/"+original.ID+"/clone", map[string]any{"key": "custom-policy", "name": "Custom policy"})
	if w.Code != 201 {
		t.Fatal("clone failed", w.Code, w.Body.String())
	}
	var clone CreationPolicyVersion
	json.Unmarshal(w.Body.Bytes(), &clone)
	tx, err = workspaceTx(ctx, pool, workspace)
	if err != nil {
		t.Fatal(err)
	}
	_, err = tx.Exec(ctx, `UPDATE creation_policy_versions SET status='published' WHERE id=$1`, clone.VersionID)
	tx.Rollback(ctx)
	if err == nil {
		t.Fatal("draft version modified in-place")
	}
	tx, err = workspaceTx(ctx, pool, workspace)
	if err != nil {
		t.Fatal(err)
	}
	tag, err := tx.Exec(ctx, `UPDATE creation_policy_sets SET name='edited' WHERE id=$1`, original.ID)
	tx.Rollback(ctx)
	if err != nil || tag.RowsAffected() != 0 {
		t.Fatal("platform RLS failed", err)
	}
	w = creationPolicyRequest(api, p, "POST", prefix+"/creation-policies/"+clone.ID+"/publish", map[string]any{"workflow": "content.generate@1", "expected_version": clone.Version})
	if w.Code != 200 {
		t.Fatal("publish failed", w.Code, w.Body.String())
	}
	var published CreationPolicyVersion
	json.Unmarshal(w.Body.Bytes(), &published)
	w = creationPolicyRequest(api, p, "PATCH", prefix+"/creation-policies/"+clone.ID, map[string]any{"expected_version": clone.Version, "name": "stale"})
	if w.Code != 409 {
		t.Fatal("stale update accepted", w.Code)
	}
	bindingPath := prefix + "/workflow-creation-policy-bindings/content.generate@1"
	w = creationPolicyRequest(api, p, "PUT", bindingPath, map[string]any{"policy_version_id": clone.VersionID})
	if w.Code != 404 {
		t.Fatal("draft version bound", w.Code, w.Body.String())
	}
	w = creationPolicyRequest(api, p, "PUT", bindingPath, map[string]any{"policy_version_id": published.VersionID, "expected_policy_version_id": baseline["version_id"]})
	if w.Code != 200 {
		t.Fatal("bind failed", w.Code, w.Body.String())
	}
	frozen, err := api.Freeze(ctx, workspace, "content.generate@1")
	if err != nil || frozen["version_id"] != published.VersionID {
		t.Fatal("override not selected", err)
	}
	w = creationPolicyRequest(api, p, "POST", prefix+"/creation-policies/"+clone.ID+"/disable", map[string]any{})
	if w.Code != 200 {
		t.Fatal("disable failed", w.Code, w.Body.String())
	}
	if _, err = api.FrozenVersion(ctx, workspace, published.VersionID); err != nil {
		t.Fatal("disable revoked historical version", err)
	}
	foreign := uuid.NewString()
	if _, err = api.FrozenVersion(ctx, foreign, published.VersionID); err == nil {
		t.Fatal("cross workspace read accepted")
	}
	w = creationPolicyRequest(api, p, "DELETE", bindingPath, nil)
	if w.Code != 204 {
		t.Fatal("delete override failed", w.Code, w.Body.String())
	}
	restored, err := api.Freeze(ctx, workspace, "content.generate@1")
	if err != nil || restored["version_id"] != baseline["version_id"] {
		t.Fatal("default not restored", err)
	}
	w = creationPolicyRequest(api, p, "GET", bindingPath+"/history", nil)
	if w.Code != 200 || !bytes.Contains(w.Body.Bytes(), []byte(published.VersionID)) {
		t.Fatal("history removed with binding", w.Code, w.Body.String())
	}
	r := httptest.NewRequest("GET", "/internal/v1/creation-policies/"+published.VersionID+"?workspace_id="+workspace, nil)
	r.Header.Set("Authorization", "Bearer internal-test")
	w = httptest.NewRecorder()
	api.ServeHTTP(w, r)
	if w.Code != 404 {
		t.Fatal("internal frozen read failed", w.Code, w.Body.String())
	}
}
