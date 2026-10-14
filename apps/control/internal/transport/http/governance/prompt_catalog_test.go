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

var testPromptVariables = json.RawMessage(`{"type":"object","properties":{"context":{"type":"string"}},"required":["context"],"additionalProperties":false}`)
var testPromptOutput = json.RawMessage(`{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":false}`)

func testPrompt() PromptVersion {
	return PromptVersion{NodeKey: "content.plan", Messages: []PromptMessage{{Role: "system", Content: "整理 {context}。Example: {\"text\": \"ok\"}"}}, VariablesSchema: testPromptVariables, OutputSchema: testPromptOutput, RendererVersion: "literal-v1"}
}
func TestPromptContractAndLiteralPreview(t *testing.T) {
	v := testPrompt()
	if err := validatePromptContract(v, testPromptVariables, testPromptOutput, "literal-v1"); err != nil {
		t.Fatal(err)
	}
	messages, err := renderPromptPreview(v, map[string]any{"context": "literal {unknown}"})
	if err != nil {
		t.Fatal(err)
	}
	if messages[0].Content != "整理 literal {unknown}。Example: {\"text\": \"ok\"}" {
		t.Fatal(messages)
	}
	for name, change := range map[string]func(*PromptVersion){"unknown variable": func(v *PromptVersion) { v.Messages[0].Content = "{unknown}" }, "missing variable": func(v *PromptVersion) { v.Messages[0].Content = "constant" }, "invalid role": func(v *PromptVersion) { v.Messages[0].Role = "tool" }, "unsupported renderer": func(v *PromptVersion) { v.RendererVersion = "jinja2" }, "wrong schema": func(v *PromptVersion) { v.OutputSchema = json.RawMessage(`{"type":"string"}`) }} {
		t.Run(name, func(t *testing.T) {
			candidate := testPrompt()
			change(&candidate)
			if err := validatePromptContract(candidate, testPromptVariables, testPromptOutput, "literal-v1"); err == nil {
				t.Fatal("invalid contract accepted")
			}
		})
	}
	if _, err := renderPromptPreview(v, map[string]any{}); err == nil {
		t.Fatal("missing required variable accepted")
	}
	if _, err := renderPromptPreview(v, map[string]any{"context": 3}); err == nil {
		t.Fatal("wrong variable type accepted")
	}
	if _, err := compilePromptSchema(json.RawMessage(`{"$ref":"https://example.invalid/schema.json"}`)); err == nil {
		t.Fatal("external schema reference accepted")
	}
}
func TestPromptCanonicalJSONAndHash(t *testing.T) {
	raw, err := canonicalPromptJSON(map[string]any{"z": "<>& \u2028 汉字", "a": json.RawMessage(`{"b":2,"a":1}`)})
	if err != nil {
		t.Fatal(err)
	}
	if string(raw) != "{\"a\":{\"a\":1,\"b\":2},\"z\":\"<>& \u2028 汉字\"}" {
		t.Fatalf("unexpected canonical JSON: %s", raw)
	}
	for input, want := range map[string]string{"1.0": "1.0", "0.00001": "1e-05", "1e6": "1000000.0", "1e16": "1e+16", "-0.0": "-0.0", "123456789012345678901": "123456789012345678901"} {
		raw, err := canonicalPromptJSON(json.RawMessage(input))
		if err != nil || string(raw) != want {
			t.Fatalf("canonical number %s: %s %v", input, raw, err)
		}
	}
	v := testPrompt()
	hash, err := promptHash(v)
	if err != nil {
		t.Fatal(err)
	}
	v.NodeKey = "other"
	v.Key = "renamed"
	other, err := promptHash(v)
	if err != nil || hash != other {
		t.Fatal("identity metadata must not participate in payload hash")
	}
	v.Messages[0].Content += "changed"
	changed, _ := promptHash(v)
	if hash == changed {
		t.Fatal("payload change did not change hash")
	}
}
func TestPromptAPIAuthenticationAndRBAC(t *testing.T) {
	api := NewPromptAPI(nil, nil, "internal-secret")
	for _, path := range []string{"/api/v2/workspaces/" + uuid.NewString() + "/prompts", "/internal/v1/prompts/" + uuid.NewString()} {
		w := httptest.NewRecorder()
		api.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		expected := 401
		if strings.HasPrefix(path, "/internal/") {
			expected = 404
		}
		if w.Code != expected {
			t.Fatalf("authentication: %d", w.Code)
		}
	}
	p := Principal{UserID: uuid.NewString(), WorkspaceID: uuid.NewString(), Role: "viewer"}
	r := httptest.NewRequest("POST", "/api/v2/workspaces/"+p.WorkspaceID+"/prompts", strings.NewReader(`{}`))
	r = r.WithContext(WithPrincipal(r.Context(), p))
	w := httptest.NewRecorder()
	api.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatalf("viewer mutation: %d", w.Code)
	}
}
func promptTestRequest(api *PromptAPI, p Principal, method, path string, body any) *httptest.ResponseRecorder {
	raw, _ := json.Marshal(body)
	r := httptest.NewRequest(method, path, bytes.NewReader(raw))
	r = r.WithContext(WithPrincipal(r.Context(), p))
	w := httptest.NewRecorder()
	api.ServeHTTP(w, r)
	return w
}
func TestPromptPostgresCatalogLifecycle(t *testing.T) {
	pool := identityTestPool(t)
	ctx := context.Background()
	api := NewPromptAPI(pool, nil, "internal-test")
	workspace := uuid.NewString()
	user := uuid.NewString()
	tx, err := workspaceTx(ctx, pool, workspace)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, `INSERT INTO workspaces(id,name) VALUES($1,'prompt catalog test')`, workspace); err == nil {
		_, err = tx.Exec(ctx, `INSERT INTO users(id,workspace_id,email,role) VALUES($1,$2,$3,'owner')`, user, workspace, user+"@invalid.test")
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		t.Fatal(err)
	}
	p := Principal{UserID: user, WorkspaceID: workspace, Role: "owner"}
	prefix := "/api/v2/workspaces/" + workspace
	frozen, err := api.Freeze(ctx, workspace, "content.generate@1")
	if err != nil {
		t.Fatal(err)
	}
	if len(frozen.Bindings) != 4 {
		t.Fatalf("expected complete default pack: %+v", frozen)
	}
	all := map[string]string{}
	for _, workflow := range []string{"novel.write@1", "content.generate@1", "content.adapt@1", "memory.refresh@1", "media.video@1", "media.image@1", "media.speech@1", "media.music@1", "media.sound_effect@1"} {
		binding, e := api.Freeze(ctx, workspace, workflow)
		if e != nil {
			t.Fatal(e)
		}
		required, _ := workflowPromptNodes(workflow)
		if len(binding.Bindings) != len(required) {
			t.Fatalf("incomplete workflow %s", workflow)
		}
		for node, id := range binding.Bindings {
			all[node] = id
		}
	}
	if len(all) != 22 {
		t.Fatalf("expected 22 unique node bindings, got %d", len(all))
	}
	var superuser bool
	if err = pool.QueryRow(ctx, `SELECT rolsuper FROM pg_roles WHERE rolname=current_user`).Scan(&superuser); err != nil {
		t.Fatal(err)
	}
	if superuser {
		t.Fatal("prompt RLS integration must use a non-superuser database role")
	}
	for node, id := range all {
		v, e := api.FrozenVersion(ctx, workspace, id)
		if e != nil {
			t.Fatal(e)
		}
		hash, e := promptHash(v)
		if e != nil || hash != v.ContentHash {
			t.Fatalf("cross-language hash mismatch %s %v", node, e)
		}
	}
	var defaultPrompt PromptVersion
	for node, id := range frozen.Bindings {
		v, e := api.FrozenVersion(ctx, workspace, id)
		if e != nil {
			t.Fatal(e)
		}
		hash, e := promptHash(v)
		if e != nil || hash != v.ContentHash {
			t.Fatalf("cross-language seed hash mismatch %s: %v %s %s", node, e, hash, v.ContentHash)
		}
		if node == "content.plan" {
			defaultPrompt = v
		}
	}
	r := httptest.NewRequest("GET", "/internal/v1/prompts/"+defaultPrompt.VersionID+"?workspace_id="+workspace, nil)
	r.Header.Set("Authorization", "Bearer internal-test")
	internal := httptest.NewRecorder()
	api.ServeHTTP(internal, r)
	if internal.Code != 404 {
		t.Fatalf("internal frozen prompt %d %s", internal.Code, internal.Body.String())
	}
	tx, err = workspaceTx(ctx, pool, workspace)
	if err != nil {
		t.Fatal(err)
	}
	tag, err := tx.Exec(ctx, `UPDATE prompts SET name='tenant edited platform' WHERE id=$1`, defaultPrompt.ID)
	if err != nil || tag.RowsAffected() != 0 {
		t.Fatalf("platform identity not protected by RLS: %d %v", tag.RowsAffected(), err)
	}
	tx.Rollback(ctx)
	w := promptTestRequest(api, p, "POST", prefix+"/prompts/"+defaultPrompt.ID+"/disable", map[string]any{})
	if w.Code != 409 {
		t.Fatalf("platform mutation status %d %s", w.Code, w.Body.String())
	}
	w = promptTestRequest(api, p, "POST", prefix+"/prompts/"+defaultPrompt.ID+"/clone", map[string]any{"key": "custom-plan", "name": "Custom plan"})
	if w.Code != 201 {
		t.Fatalf("clone %d %s", w.Code, w.Body.String())
	}
	var cloned PromptVersion
	if err = json.Unmarshal(w.Body.Bytes(), &cloned); err != nil {
		t.Fatal(err)
	}
	w = promptTestRequest(api, p, "PATCH", prefix+"/prompts/"+cloned.ID, map[string]any{"messages": []PromptMessage{{Role: "user", Content: "{unknown}"}}})
	if w.Code != 422 {
		t.Fatalf("unknown variable publish accepted %d", w.Code)
	}
	w = promptTestRequest(api, p, "POST", prefix+"/prompts/"+cloned.ID+"/publish", map[string]any{})
	if w.Code != 200 {
		t.Fatalf("publish %d %s", w.Code, w.Body.String())
	}
	var published PromptVersion
	json.Unmarshal(w.Body.Bytes(), &published)
	w = promptTestRequest(api, p, "POST", prefix+"/prompts/"+cloned.ID+"/disable", map[string]any{})
	if w.Code != 200 {
		t.Fatalf("disable %d %s", w.Code, w.Body.String())
	}
	historic, err := api.FrozenVersion(ctx, workspace, published.VersionID)
	if err != nil || historic.VersionID != published.VersionID {
		t.Fatalf("historic version disabled: %v", err)
	}
	entries := []PromptPackEntry{}
	for node, id := range frozen.Bindings {
		if node == "content.plan" {
			id = published.VersionID
		}
		entries = append(entries, PromptPackEntry{NodeKey: node, PromptVersionID: id})
	}
	w = promptTestRequest(api, p, "POST", prefix+"/prompt-packs", map[string]any{"key": "duplicate-pack", "name": "Conflict", "entries": append(append([]PromptPackEntry{}, entries...), entries[0])})
	if w.Code != 422 {
		t.Fatalf("duplicate node accepted %d", w.Code)
	}
	w = promptTestRequest(api, p, "POST", prefix+"/prompt-packs", map[string]any{"key": "custom-content", "name": "Custom content", "entries": entries})
	if w.Code != 201 {
		t.Fatalf("pack create %d %s", w.Code, w.Body.String())
	}
	var pack PromptPackVersion
	json.Unmarshal(w.Body.Bytes(), &pack)
	w = promptTestRequest(api, p, "POST", prefix+"/prompt-packs/"+pack.ID+"/publish", map[string]any{"workflow": "content.generate@1"})
	if w.Code != 200 {
		t.Fatalf("pack publish %d %s", w.Code, w.Body.String())
	}
	json.Unmarshal(w.Body.Bytes(), &pack)
	tx, err = workspaceTx(ctx, pool, workspace)
	if err != nil {
		t.Fatal(err)
	}
	_, err = tx.Exec(ctx, `INSERT INTO prompt_pack_entries(pack_version_id,node_key,prompt_version_id) VALUES($1,$2,$3)`, pack.VersionID, entries[0].NodeKey, entries[0].PromptVersionID)
	tx.Rollback(ctx)
	if err == nil {
		t.Fatal("existing pack membership could be changed by insert")
	}
	bindingPath := prefix + "/workflow-prompt-bindings/content.generate@1"
	w = promptTestRequest(api, p, "PUT", bindingPath, map[string]any{"pack_version_id": pack.VersionID})
	if w.Code != 200 {
		t.Fatalf("bind %d %s", w.Code, w.Body.String())
	}
	selected, err := api.Freeze(ctx, workspace, "content.generate@1")
	if err != nil || selected.PackVersionID != pack.VersionID || selected.Bindings["content.plan"] != published.VersionID {
		t.Fatalf("selected binding %+v %v", selected, err)
	}
	w = promptTestRequest(api, p, "POST", bindingPath+"/rollback", map[string]any{"pack_version_id": frozen.PackVersionID})
	if w.Code != 200 {
		t.Fatalf("rollback %d %s", w.Code, w.Body.String())
	}
	rolled, err := api.Freeze(ctx, workspace, "content.generate@1")
	if err != nil || rolled.PackVersionID != frozen.PackVersionID {
		t.Fatal("rollback did not restore default binding")
	}
	if selected.Bindings["content.plan"] != published.VersionID {
		t.Fatal("rollback mutated prior snapshot")
	}
	w = promptTestRequest(api, p, "DELETE", bindingPath, nil)
	if w.Code != 204 {
		t.Fatalf("delete override %d %s", w.Code, w.Body.String())
	}
	rolled, err = api.Freeze(ctx, workspace, "content.generate@1")
	if err != nil || rolled.PackVersionID != frozen.PackVersionID {
		t.Fatal("deleting override did not preserve platform default")
	}
	foreign := p
	foreign.WorkspaceID = uuid.NewString()
	w = promptTestRequest(api, foreign, "GET", "/api/v2/workspaces/"+foreign.WorkspaceID+"/prompts/"+cloned.ID, nil)
	if w.Code != 404 {
		t.Fatalf("cross-scope prompt read %d", w.Code)
	}
	tx, err = workspaceTx(ctx, pool, workspace)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, `UPDATE prompt_versions SET status='draft' WHERE id=$1`, published.VersionID); err == nil {
		t.Fatal("immutable version update accepted")
	}
	tx.Rollback(ctx)
	tx, err = workspaceTx(ctx, pool, workspace)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, `DELETE FROM prompt_versions WHERE id=$1`, published.VersionID); err == nil {
		t.Fatal("immutable version delete accepted")
	}
}
