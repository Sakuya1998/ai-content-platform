package governancehttp

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestGovernanceSecretCipher(t *testing.T) {
	g := NewGovernanceAPI(nil, nil, strings.Repeat("ab", 32), "internal")
	encrypted, err := g.encryptSecret("test-secret")
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(encrypted, []byte("test-secret")) {
		t.Fatal("plaintext stored")
	}
	decoded, err := g.decryptSecret(encrypted)
	if err != nil || decoded != "test-secret" {
		t.Fatalf("round trip: %q %v", decoded, err)
	}
	encrypted[len(encrypted)-1] ^= 1
	if _, err = g.decryptSecret(encrypted); err == nil {
		t.Fatal("tampered cipher accepted")
	}
	if _, err = NewGovernanceAPI(nil, nil, "", "internal").encryptSecret("secret"); err == nil {
		t.Fatal("missing encryption key accepted")
	}
}

func TestIdentityUnauthenticatedAndInternalToken(t *testing.T) {
	a := NewIdentityAPI(nil, IdentitySettings{Enabled: true})
	w := httptest.NewRecorder()
	a.AuthenticatedHandler(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { t.Fatal("unauthenticated handler reached") })).ServeHTTP(w, httptest.NewRequest("GET", "/api/v2/projects", nil))
	if w.Code != 401 {
		t.Fatalf("got %d", w.Code)
	}
	w = httptest.NewRecorder()
	NewGovernanceAPI(nil, a, "", "internal").ServeHTTP(w, httptest.NewRequest("GET", "/internal/v1/model-routes/"+uuid.NewString(), nil))
	if w.Code != 404 {
		t.Fatalf("internal got %d", w.Code)
	}
}

func TestIdentityCSRFAndOrigin(t *testing.T) {
	r := httptest.NewRequest("POST", "/api/v2/projects", nil)
	r.AddCookie(&http.Cookie{Name: identityCookie, Value: "session"})
	if identityCheckCSRF(r) {
		t.Fatal("missing CSRF accepted")
	}
	token := identityCSRF("session")
	r.AddCookie(&http.Cookie{Name: "novel_agent_csrf", Value: token})
	r.Header.Set("X-CSRF-Token", token)
	if !identityCheckCSRF(r) {
		t.Fatal("valid CSRF rejected")
	}
	r.Header.Set("X-CSRF-Token", identityCSRF("other"))
	if identityCheckCSRF(r) {
		t.Fatal("foreign session CSRF accepted")
	}
	a := NewIdentityAPI(nil, IdentitySettings{AllowedOrigins: []string{"https://trusted.example"}})
	r.Header.Set("Origin", "https://untrusted.example")
	if a.OriginAllowed(r) {
		t.Fatal("untrusted origin accepted")
	}
	r.Header.Set("Origin", "https://trusted.example")
	if !a.OriginAllowed(r) {
		t.Fatal("trusted origin rejected")
	}
}

func TestGovernanceResourcePayloadBoundsAndProviders(t *testing.T) {
	if _, err := normalizeGovernancePayload("styles", json.RawMessage(`{"prompt":"execute arbitrary code"}`)); err == nil {
		t.Fatal("uncontrolled style prompt accepted")
	}
	if _, err := normalizeGovernancePayload("quality_policies", json.RawMessage(`{"gate_threshold":101}`)); err == nil {
		t.Fatal("out of range gate accepted")
	}
	payload, err := normalizeGovernancePayload("quality_policies", json.RawMessage(`{}`))
	if err != nil || !bytes.Contains(payload, []byte(`"gate_threshold":70`)) {
		t.Fatalf("quality defaults %s %v", payload, err)
	}
	payload, err = normalizeGovernancePayload("creative_templates", json.RawMessage(`{"intensity":{"action":999},"point_of_view":"invalid"}`))
	if err != nil || !bytes.Contains(payload, []byte(`"action":5`)) || !bytes.Contains(payload, []byte(`"point_of_view":"third_limited"`)) {
		t.Fatalf("creative defaults %s %v", payload, err)
	}
	for _, provider := range []string{"openai", "anthropic", "gemini", "deepseek", "qwen", "xai", "groq", "moonshot", "zhipu", "volcengine", "siliconflow", "openrouter", "ollama", "openai_compatible"} {
		if err := validateModelProfile(modelProfileWrite{Name: "profile", Provider: provider, BaseURL: "http://localhost/v1"}); err != nil {
			t.Fatalf("legacy provider %s: %v", provider, err)
		}
	}
}

func TestGovernanceBuiltInResources(t *testing.T) {
	for _, key := range []string{"jin_yong", "gu_long", "murakami", "yu_hua"} {
		v, ok := builtinGovernanceStyle("workspace", "system_style_"+key)
		if !ok || v.Status != "published" || v.Version != 1 || v.ContentHash == "" {
			t.Fatalf("missing builtin %s", key)
		}
		if _, err := normalizeGovernancePayload("styles", v.Payload); err != nil {
			t.Fatalf("invalid builtin %s %v", key, err)
		}
	}
}

func TestGovernancePostgresFrozenProfilesAndResourceLifecycle(t *testing.T) {
	pool := identityTestPool(t)
	a := NewIdentityAPI(pool, IdentitySettings{Enabled: true})
	g := NewGovernanceAPI(pool, a, strings.Repeat("ab", 32), "internal-test")
	username := "govern_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	w := identityRequest(a, "POST", "/api/v2/auth/register", `{"username":"`+username+`","password":"a-secure-password"}`, nil)
	if w.Code != 201 {
		t.Fatal(w.Body.String())
	}
	cookie := w.Result().Cookies()[0]
	var registered struct {
		User Principal `json:"user"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &registered)
	workspace := registered.User.WorkspaceID
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), "DELETE FROM workspaces WHERE id=$1", workspace) })
	profileBody := `{"name":"test profile","provider":"openai_compatible","base_url":"http://localhost:9999/v1","api_key":"private-test-key","chat_models":["chat-test"],"embedding_models":["embed-test"]}`
	w = identityRequest(g, "POST", "/api/v2/model-settings/profiles", profileBody, cookie)
	if w.Code != 201 {
		t.Fatalf("profile: %d %s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "private-test-key") {
		t.Fatal("public secret leaked")
	}
	var profile ModelProfile
	_ = json.Unmarshal(w.Body.Bytes(), &profile)
	targets := map[string]RouteTarget{"creative": {ProfileID: profile.ID, ModelName: "chat-test"}, "analysis": {ProfileID: profile.ID, ModelName: "chat-test", FallbackProfileID: profile.ID, FallbackModelName: "chat-test"}, "embedding": {ProfileID: profile.ID, ModelName: "embed-test"}}
	payload, _ := json.Marshal(targets)
	w = identityRequest(g, "PUT", "/api/v2/model-settings/routes", string(payload), cookie)
	if w.Code != 200 {
		t.Fatalf("routes %d %s", w.Code, w.Body.String())
	}
	var saved struct {
		ID string `json:"route_version_id"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &saved)
	w = identityRequest(g, "PUT", "/api/v2/model-settings/profiles/"+profile.ID, strings.Replace(profileBody, "private-test-key", "new-private-test-key", 1), cookie)
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	frozen, err := g.ResolveFrozenRoutes(context.Background(), workspace, saved.ID)
	if err != nil {
		t.Fatal(err)
	}
	if frozen.Routes["creative"].APIKey != "private-test-key" || len(frozen.Routes["analysis"].Fallback) != 1 {
		t.Fatalf("not frozen: %#v", frozen)
	}
	if _, err = g.ResolveFrozenRoutes(context.Background(), uuid.NewString(), saved.ID); err == nil {
		t.Fatal("cross workspace route visible")
	}
	w = identityRequest(g, "DELETE", "/api/v2/model-settings/profiles/"+profile.ID, "", cookie)
	if w.Code != 409 {
		t.Fatalf("used profile deletion %d", w.Code)
	}
	path := "/api/v2/workspaces/" + workspace + "/styles"
	w = identityRequest(g, "POST", path, `{"key":"test-style","name":"Test style","payload":{"pacing":"brisk"}}`, cookie)
	if w.Code != 201 {
		t.Fatal(w.Body.String())
	}
	var resource ResourceVersion
	_ = json.Unmarshal(w.Body.Bytes(), &resource)
	w = identityRequest(g, "POST", path+"/"+resource.ID+"/publish", `{"expected_version":1}`, cookie)
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	var published ResourceVersion
	_ = json.Unmarshal(w.Body.Bytes(), &published)
	if published.Version != 2 || published.Status != "published" {
		t.Fatal(w.Body.String())
	}
	w = identityRequest(g, "PATCH", path+"/"+resource.ID, `{"expected_version":1,"payload":{"pacing":"slow"}}`, cookie)
	if w.Code != 409 {
		t.Fatalf("stale edit %d", w.Code)
	}
	w = identityRequest(g, "PATCH", path+"/"+resource.ID, `{"expected_version":2,"payload":{"pacing":"slow"}}`, cookie)
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	original, err := g.FrozenResource(context.Background(), workspace, published.VersionID)
	if err != nil || original.Status != "published" || !bytes.Contains(original.Payload, []byte("brisk")) {
		t.Fatalf("published mutation %#v %v", original, err)
	}
	w = identityRequest(g, "POST", path+"/"+resource.ID+"/clone", `{"key":"copied-style","name":"Copy"}`, cookie)
	if w.Code != 201 {
		t.Fatal(w.Body.String())
	}
	snapshot, err := g.FreezeSnapshot(context.Background(), workspace, []string{published.VersionID})
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(snapshot, []byte("private-test-key")) {
		t.Fatal("snapshot contains secret")
	}
	qualityPath := "/api/v2/workspaces/" + workspace + "/quality-policies"
	w = identityRequest(g, "POST", qualityPath, `{"key":"quality","name":"Quality","payload":{}}`, cookie)
	if w.Code != 201 {
		t.Fatal(w.Body.String())
	}
	var policy ResourceVersion
	_ = json.Unmarshal(w.Body.Bytes(), &policy)
	w = identityRequest(g, "POST", qualityPath+"/"+policy.ID+"/publish", `{}`, cookie)
	if w.Code != 200 {
		t.Fatalf("publish normalized quality %d %s", w.Code, w.Body.String())
	}
	w = identityRequest(g, "POST", "/api/v2/model-settings/profiles", `{"name":"No secret","provider":"ollama","base_url":"http://localhost:11434/v1"}`, cookie)
	if w.Code != 201 {
		t.Fatalf("no secret profile %d %s", w.Code, w.Body.String())
	}
	w = identityRequest(g, "POST", path+"/system_style_jin_yong/clone", `{"key":"custom_builtin","name":"Custom built-in"}`, cookie)
	if w.Code != 201 {
		t.Fatalf("clone builtin %d %s", w.Code, w.Body.String())
	}
	promptPath := "/api/v2/workspaces/" + workspace + "/prompt-packs"
	w = identityRequest(g, "POST", promptPath, `{"key":"extra-prompts","name":"Extra prompts","payload":{"templates":{"writer":"Keep the scene coherent."}}}`, cookie)
	if w.Code != 201 {
		t.Fatal(w.Body.String())
	}
	var prompt ResourceVersion
	_ = json.Unmarshal(w.Body.Bytes(), &prompt)
	w = identityRequest(g, "POST", promptPath+"/"+prompt.ID+"/publish", `{}`, cookie)
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	_ = json.Unmarshal(w.Body.Bytes(), &prompt)
	base := map[string]any{"schema_version": "1.0", "workflow": "novel.write@1", "skills": []string{"legacy-novel@1"}, "prompt_packs": []string{"novel@1"}, "tools": []string{}, "mcp_policies": []string{}, "model_routes": map[string]string{}, "schema_versions": map[string]string{"content_ir": "1.0"}}
	g.SetPromptCatalog(NewPromptAPI(pool, a, "test-internal"))
	g.SetCreationPolicies(NewCreationPolicyAPI(pool, a, "test-internal"))
	capability, err := g.FreezeCapabilitySnapshot(context.Background(), workspace, base)
	if err != nil {
		t.Fatal(err)
	}
	if len(capability) != 10 || len(capability["prompt_packs"].([]string)) != 1 || len(capability["prompt_bindings"].(map[string]string)) != 13 {
		t.Fatalf("invalid frozen snapshot %#v", capability)
	}
	policyRef, ok := capability["creation_policy"].(map[string]string)
	if !ok || policyRef["version_id"] == "" || len(policyRef["content_hash"]) != 64 {
		t.Fatalf("missing creation policy snapshot %#v", capability)
	}
	input, err := g.FreezeContentInput(context.Background(), workspace, map[string]any{"title": "Title", "style": "jin_yong", "quality_policy_id": policy.ID})
	if err != nil {
		t.Fatal(err)
	}
	if len(input) != 4 || input["title"] != nil || input["style_snapshot"] == nil || input["model_route_version_id"] != nil {
		t.Fatalf("invalid frozen input %#v", input)
	}
	mutationTx, e := workspaceTx(context.Background(), pool, workspace)
	if e != nil {
		t.Fatal(e)
	}
	defer mutationTx.Rollback(context.Background())
	if _, err = mutationTx.Exec(context.Background(), "UPDATE governance_model_profile_versions SET name='tampered' WHERE id=$1", profile.VersionID); err == nil {
		t.Fatal("immutable profile mutation accepted")
	}
}

func TestGovernanceConnectionTestUsesFrozenProfileAndMasksErrors(t *testing.T) {
	requests := 0
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.URL.Path != "/v1/chat/completions" || r.Header.Get("Authorization") != "Bearer local-test-key" {
			t.Error("incorrect provider request")
		}
		writeJSON(w, 200, map[string]any{"choices": []any{map[string]any{"message": map[string]string{"content": "OK"}}}})
	}))
	defer provider.Close()
	g := NewGovernanceAPI(nil, nil, strings.Repeat("ab", 32), "")
	key, err := g.encryptSecret("local-test-key")
	if err != nil {
		t.Fatal(err)
	}
	p := ModelProfile{ID: uuid.NewString(), Provider: "openai_compatible", BaseURL: provider.URL + "/v1", ChatModels: []string{"test-chat"}, EncryptedKey: key}
	r := httptest.NewRequest("POST", "/test", strings.NewReader(`{"kind":"chat","model_name":"test-chat"}`))
	w := httptest.NewRecorder()
	g.testModelProfile(w, r, p)
	if w.Code != 200 || requests != 1 || strings.Contains(w.Body.String(), "local-test-key") {
		t.Fatalf("connection test %d %s", w.Code, w.Body.String())
	}
	r = httptest.NewRequest("POST", "/test", strings.NewReader(`{"kind":"chat","model_name":"unknown"}`))
	w = httptest.NewRecorder()
	g.testModelProfile(w, r, p)
	if w.Code != 422 || requests != 1 {
		t.Fatal("unknown model sent to provider")
	}
}

func identityTestPool(t *testing.T) *pgxpool.Pool {
	t.Helper()
	url := os.Getenv("MQ_TEST_CONTROL_DATABASE_URL")
	if url == "" {
		t.Skip("MQ_TEST_CONTROL_DATABASE_URL is not configured")
	}
	pool, err := pgxpool.New(context.Background(), url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)
	return pool
}

func identityRequest(h http.Handler, method, path, body string, cookie *http.Cookie) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	if cookie != nil {
		r.AddCookie(cookie)
		csrf := identityCSRF(cookie.Value)
		r.AddCookie(&http.Cookie{Name: "novel_agent_csrf", Value: csrf})
		r.Header.Set("X-CSRF-Token", csrf)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func TestIdentityPostgresSessionRolesAndIsolation(t *testing.T) {
	pool := identityTestPool(t)
	a := NewIdentityAPI(pool, IdentitySettings{Enabled: true, SessionHours: 1})
	username := "owner_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	w := identityRequest(a, "POST", "/api/v2/auth/register", `{"username":"`+username+`","password":"a-secure-password","workspace_name":"Identity test"}`, nil)
	if w.Code != 201 {
		t.Fatalf("register %d %s", w.Code, w.Body.String())
	}
	cookies := w.Result().Cookies()
	if len(cookies) != 2 || !cookies[0].HttpOnly {
		t.Fatal("missing secure session cookie")
	}
	cookie := cookies[0]
	var response struct {
		User Principal `json:"user"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &response); err != nil {
		t.Fatal(err)
	}
	p := response.User
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), "DELETE FROM workspaces WHERE id=$1", p.WorkspaceID) })
	w = identityRequest(a, "PATCH", "/api/v2/workspaces/"+p.WorkspaceID+"/members/"+p.UserID, `{"role":"viewer"}`, cookie)
	if w.Code != 409 {
		t.Fatalf("last owner %d %s", w.Code, w.Body.String())
	}
	guard := a.AuthenticatedHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]string{"workspace": r.Header.Get("X-Workspace-ID")})
	}))
	r := httptest.NewRequest("POST", "/api/v2/projects", strings.NewReader(`{"workspace_id":"`+uuid.NewString()+`"}`))
	r.AddCookie(cookie)
	csrf := identityCSRF(cookie.Value)
	r.AddCookie(&http.Cookie{Name: "novel_agent_csrf", Value: csrf})
	r.Header.Set("X-CSRF-Token", csrf)
	out := httptest.NewRecorder()
	guard.ServeHTTP(out, r)
	if out.Code != 403 {
		t.Fatalf("spoof accepted: %d", out.Code)
	}
	w = identityRequest(a, "POST", "/api/v2/auth/logout", "", cookie)
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	w = identityRequest(a, "GET", "/api/v2/auth/me", "", cookie)
	if w.Code != 401 {
		t.Fatalf("revoked session %d", w.Code)
	}
	w = identityRequest(a, "POST", "/api/v2/auth/login", `{"username":"`+username+`","password":"wrong"}`, nil)
	if w.Code != 401 {
		t.Fatalf("bad password %d", w.Code)
	}
}

func TestIdentityPostgresViewerRevokeAndConcurrentOwners(t *testing.T) {
	pool := identityTestPool(t)
	a := NewIdentityAPI(pool, IdentitySettings{Enabled: true})
	username := "roles_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	w := identityRequest(a, "POST", "/api/v2/auth/register", `{"username":"`+username+`","password":"a-secure-password"}`, nil)
	if w.Code != 201 {
		t.Fatal(w.Body.String())
	}
	cookie := w.Result().Cookies()[0]
	var response struct {
		User Principal `json:"user"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &response)
	p := response.User
	t.Cleanup(func() { _, _ = pool.Exec(context.Background(), "DELETE FROM workspaces WHERE id=$1", p.WorkspaceID) })
	memberName := "viewer_" + strings.ReplaceAll(uuid.NewString(), "-", "")
	memberPath := "/api/v2/workspaces/" + p.WorkspaceID + "/members"
	w = identityRequest(a, "POST", memberPath, `{"username":"`+memberName+`","password":"a-secure-password","role":"viewer"}`, cookie)
	if w.Code != 201 {
		t.Fatal(w.Body.String())
	}
	var member struct {
		User Principal `json:"user"`
	}
	_ = json.Unmarshal(w.Body.Bytes(), &member)
	w = identityRequest(a, "POST", "/api/v2/auth/login", `{"username":"`+memberName+`","password":"a-secure-password"}`, nil)
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	viewerCookie := w.Result().Cookies()[0]
	guard := a.AuthenticatedHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(200) }))
	w = identityRequest(guard, "POST", "/api/v2/projects", `{}`, viewerCookie)
	if w.Code != 403 {
		t.Fatalf("viewer write %d", w.Code)
	}
	w = identityRequest(guard, "GET", "/api/v2/projects", "", viewerCookie)
	if w.Code != 200 {
		t.Fatalf("viewer read %d", w.Code)
	}
	w = identityRequest(a, "PATCH", memberPath+"/"+member.User.UserID, `{"role":"editor"}`, cookie)
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	w = identityRequest(guard, "GET", "/api/v2/projects", "", viewerCookie)
	if w.Code != 401 {
		t.Fatal("old session survived role mutation")
	}
	w = identityRequest(a, "POST", "/api/v2/auth/login", `{"username":"`+memberName+`","password":"a-secure-password"}`, nil)
	editorCookie := w.Result().Cookies()[0]
	w = identityRequest(guard, "POST", "/api/v2/projects", `{}`, editorCookie)
	if w.Code != 200 {
		t.Fatalf("editor write %d", w.Code)
	}
	w = identityRequest(a, "PATCH", memberPath+"/"+member.User.UserID, `{"role":"owner"}`, cookie)
	if w.Code != 200 {
		t.Fatal(w.Body.String())
	}
	w = identityRequest(a, "POST", "/api/v2/auth/login", `{"username":"`+memberName+`","password":"a-secure-password"}`, nil)
	secondOwnerCookie := w.Result().Cookies()[0]
	codes := make(chan int, 2)
	go func() {
		codes <- identityRequest(a, "PATCH", memberPath+"/"+p.UserID, `{"role":"viewer"}`, cookie).Code
	}()
	go func() {
		codes <- identityRequest(a, "PATCH", memberPath+"/"+member.User.UserID, `{"role":"viewer"}`, secondOwnerCookie).Code
	}()
	first, second := <-codes, <-codes
	if (first == 200) == (second == 200) {
		t.Fatalf("concurrent demotions %d %d", first, second)
	}
	var owners int
	if err := pool.QueryRow(context.Background(), "SELECT count(*) FROM users WHERE workspace_id=$1 AND role='owner' AND NOT disabled", p.WorkspaceID).Scan(&owners); err != nil || owners != 1 {
		t.Fatalf("owners=%d %v", owners, err)
	}
}
