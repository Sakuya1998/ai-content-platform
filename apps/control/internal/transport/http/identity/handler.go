package identityhttp

import (
	"bytes"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/identity"
)

type Settings struct {
	Enabled        bool
	CookieSecure   bool
	SessionHours   int
	AllowedOrigins []string
}
type loginBucket struct {
	count   int
	expires time.Time
}
type Handler struct {
	service       *identity.Service
	settings      Settings
	loginMu       sync.Mutex
	loginAttempts map[string]loginBucket
}

func NewHandler(service *identity.Service, settings Settings) *Handler {
	if settings.SessionHours < 1 {
		settings.SessionHours = 24
	}
	settings.AllowedOrigins = append([]string(nil), settings.AllowedOrigins...)
	return &Handler{service: service, settings: settings, loginAttempts: map[string]loginBucket{}}
}

const CookieName = "novel_agent_session"

func sessionCookie(r *http.Request) (*http.Cookie, error) {
	cookie, err := r.Cookie(CookieName)
	if err != nil {
		return r.Cookie("novel_session")
	}
	return cookie, nil
}
func CSRF(token string) string {
	hash := sha256.Sum256([]byte("novel-agent:csrf:" + token))
	return hex.EncodeToString(hash[:])
}
func CheckCSRF(r *http.Request) bool {
	session, err := sessionCookie(r)
	if err != nil {
		return false
	}
	cookie, err := r.Cookie("novel_agent_csrf")
	if err != nil {
		return false
	}
	expected := CSRF(session.Value)
	return subtle.ConstantTimeCompare([]byte(cookie.Value), []byte(expected)) == 1 && subtle.ConstantTimeCompare([]byte(r.Header.Get("X-CSRF-Token")), []byte(expected)) == 1
}
func canonicalPath(path string) string {
	for _, prefix := range []string{"/api/v2/", "/api/v1/"} {
		if strings.HasPrefix(path, prefix) {
			return "/api/" + strings.TrimPrefix(path, prefix)
		}
	}
	return strings.TrimSuffix(path, "/")
}
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func reject(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}
func databaseError(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, identity.ErrUnauthorized):
		reject(w, 401, "authentication required")
	case errors.Is(err, identity.ErrForbidden):
		reject(w, 403, "owner role required")
	case errors.Is(err, identity.ErrInvalid):
		reject(w, 422, "invalid identity input")
	case errors.Is(err, identity.ErrNotFound):
		reject(w, 404, "resource not found")
	case errors.Is(err, identity.ErrRetainOwner):
		reject(w, 409, "workspace must retain an owner")
	case errors.Is(err, identity.ErrRemoveSelf):
		reject(w, 409, "cannot remove current user")
	case errors.Is(err, identity.ErrConflict):
		reject(w, 409, "resource conflict")
	default:
		reject(w, 503, "storage unavailable")
	}
}
func decodeJSON(w http.ResponseWriter, r *http.Request, value any) bool {
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if decoder.Decode(value) != nil {
		reject(w, 400, "invalid request body")
		return false
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		reject(w, 400, "request body must contain one JSON object")
		return false
	}
	return true
}
func methodNotAllowed(w http.ResponseWriter) {
	w.Header().Set("Allow", "GET, POST, PATCH, DELETE")
	reject(w, 405, "method not allowed")
}

func (a *Handler) OriginAllowed(r *http.Request) bool {
	origin := r.Header.Get("Origin")
	if origin == "" {
		return true
	}
	for _, allowed := range a.settings.AllowedOrigins {
		if origin == allowed {
			return true
		}
	}
	scheme := "http"
	if r.TLS != nil {
		scheme = "https"
	}
	return origin == scheme+"://"+r.Host
}
func (a *Handler) loginPermitted(r *http.Request) bool {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	now := time.Now()
	a.loginMu.Lock()
	defer a.loginMu.Unlock()
	for key, bucket := range a.loginAttempts {
		if now.After(bucket.expires) {
			delete(a.loginAttempts, key)
		}
	}
	bucket := a.loginAttempts[host]
	if bucket.expires.IsZero() || now.After(bucket.expires) {
		bucket = loginBucket{expires: now.Add(5 * time.Minute)}
	}
	bucket.count++
	a.loginAttempts[host] = bucket
	return bucket.count <= 30
}
func (a *Handler) sessionPrincipal(r *http.Request) (identity.Principal, error) {
	cookie, err := sessionCookie(r)
	if err != nil {
		return identity.Principal{}, identity.ErrUnauthorized
	}
	return a.service.Authenticate(r.Context(), cookie.Value)
}
func (a *Handler) AuthenticatedHandler(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !a.settings.Enabled {
			next.ServeHTTP(w, r)
			return
		}
		p, err := a.sessionPrincipal(r)
		if err != nil {
			reject(w, 401, "authentication required")
			return
		}
		write := r.Method != http.MethodGet && r.Method != http.MethodHead && r.Method != http.MethodOptions
		if write && !a.OriginAllowed(r) {
			reject(w, 403, "origin not allowed")
			return
		}
		if write && !CheckCSRF(r) {
			reject(w, 403, "CSRF token required")
			return
		}
		if write && p.Role == "viewer" && canonicalPath(r.URL.Path) != "/api/auth/logout" {
			reject(w, 403, "editor role required")
			return
		}
		if selected := r.Header.Get("X-Workspace-ID"); selected != "" && selected != p.WorkspaceID {
			reject(w, 403, "workspace access denied")
			return
		}
		if write && r.Body != nil {
			body, err := io.ReadAll(io.LimitReader(r.Body, (32<<20)+1))
			if err != nil {
				reject(w, 400, "invalid request")
				return
			}
			if len(body) > 32<<20 {
				reject(w, 413, "request body too large")
				return
			}
			r.Body = io.NopCloser(bytes.NewReader(body))
			var payload map[string]json.RawMessage
			if json.Unmarshal(body, &payload) == nil {
				for _, key := range []string{"workspace_id", "tenant_id"} {
					if raw, ok := payload[key]; ok {
						var workspace string
						if json.Unmarshal(raw, &workspace) != nil || workspace != "" && workspace != p.WorkspaceID {
							reject(w, 403, "workspace access denied")
							return
						}
					}
				}
			}
		}
		r.Header.Set("X-Workspace-ID", p.WorkspaceID)
		next.ServeHTTP(w, r.WithContext(identity.WithPrincipal(r.Context(), p)))
	})
}
func (a *Handler) setSessionCookies(w http.ResponseWriter, token string, maxAge int) {
	http.SetCookie(w, &http.Cookie{Name: CookieName, Value: token, Path: "/", HttpOnly: true, Secure: a.settings.CookieSecure, SameSite: http.SameSiteLaxMode, MaxAge: maxAge})
	csrf := CSRF(token)
	if token == "" {
		csrf = ""
	}
	http.SetCookie(w, &http.Cookie{Name: "novel_agent_csrf", Value: csrf, Path: "/", Secure: a.settings.CookieSecure, SameSite: http.SameSiteLaxMode, MaxAge: maxAge})
}
func (a *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	path := canonicalPath(r.URL.Path)
	if path == "/api/auth/status" && r.Method == http.MethodGet {
		p, err := a.sessionPrincipal(r)
		var user any
		if err == nil {
			user = p
		}
		writeJSON(w, 200, map[string]any{"enabled": a.settings.Enabled, "user": user})
		return
	}
	if r.Method == http.MethodPost && (path == "/api/auth/register" || path == "/api/auth/login") {
		if !a.OriginAllowed(r) {
			reject(w, 403, "origin not allowed")
			return
		}
		if !a.loginPermitted(r) {
			reject(w, 429, "too many authentication attempts")
			return
		}
		var input identity.Credentials
		if !decodeJSON(w, r, &input) {
			return
		}
		var session identity.Session
		var err error
		status := 200
		if path == "/api/auth/register" {
			session, err = a.service.Register(r.Context(), input)
			status = 201
		} else {
			session, err = a.service.Login(r.Context(), input)
		}
		if err != nil {
			if errors.Is(err, identity.ErrUnauthorized) {
				reject(w, 401, "invalid credentials")
			} else {
				databaseError(w, err)
			}
			return
		}
		a.setSessionCookies(w, session.Token, a.settings.SessionHours*3600)
		writeJSON(w, status, map[string]any{"user": session.Principal, "csrf_token": CSRF(session.Token)})
		return
	}
	a.AuthenticatedHandler(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p, _ := identity.FromContext(r.Context())
		if path == "/api/auth/me" && r.Method == http.MethodGet {
			writeJSON(w, 200, map[string]any{"user": p})
			return
		}
		if path == "/api/auth/logout" && r.Method == http.MethodPost {
			cookie, err := sessionCookie(r)
			if err != nil {
				reject(w, 401, "authentication required")
				return
			}
			if err = a.service.Logout(r.Context(), p, cookie.Value); err != nil {
				databaseError(w, err)
				return
			}
			a.setSessionCookies(w, "", -1)
			writeJSON(w, 200, map[string]bool{"logged_out": true})
			return
		}
		if strings.HasPrefix(path, "/api/auth/users") {
			tail := strings.TrimPrefix(path, "/api/auth/users")
			if strings.HasSuffix(tail, "/role") && r.Method == http.MethodPut {
				tail = strings.TrimSuffix(tail, "/role")
				r.Method = http.MethodPatch
			}
			r.URL.Path = "/api/workspaces/" + p.WorkspaceID + "/members" + tail
		} else if path == "/api/audit/logs" {
			r.URL.Path = "/api/workspaces/" + p.WorkspaceID + "/audit-logs"
		} else if strings.HasPrefix(path, "/api/auth/") {
			http.NotFound(w, r)
			return
		}
		a.workspaces(w, r, p)
	})).ServeHTTP(w, r)
}
func (a *Handler) workspaces(w http.ResponseWriter, r *http.Request, p identity.Principal) {
	if _, ok := identity.FromContext(r.Context()); !ok {
		reject(w, 401, "authentication required")
		return
	}
	parts := strings.Split(strings.TrimPrefix(canonicalPath(r.URL.Path), "/api/workspaces"), "/")
	if len(parts) > 1 && parts[1] != "" && parts[1] != p.WorkspaceID {
		reject(w, 403, "workspace access denied")
		return
	}
	if r.Method != http.MethodGet && p.Role != "owner" {
		reject(w, 403, "owner role required")
		return
	}
	ctx := r.Context()
	if len(parts) < 3 {
		var item identity.Workspace
		var err error
		if r.Method == http.MethodPatch {
			var input struct {
				Name string `json:"name"`
			}
			if !decodeJSON(w, r, &input) {
				return
			}
			item, err = a.service.UpdateWorkspace(ctx, p, input.Name)
		} else if r.Method == http.MethodGet {
			item, err = a.service.Workspace(ctx, p)
		} else {
			methodNotAllowed(w)
			return
		}
		if err != nil {
			databaseError(w, err)
			return
		}
		if len(parts) == 1 {
			writeJSON(w, 200, map[string]any{"items": []identity.Workspace{item}, "has_more": false})
		} else {
			writeJSON(w, 200, item)
		}
		return
	}
	if parts[2] == "audit-logs" && r.Method == http.MethodGet {
		items, err := a.service.AuditLogs(ctx, p)
		if err != nil {
			databaseError(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"items": items})
		return
	}
	if parts[2] != "members" {
		http.NotFound(w, r)
		return
	}
	if len(parts) == 3 && r.Method == http.MethodGet {
		items, err := a.service.Members(ctx, p)
		if err != nil {
			databaseError(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"items": items, "has_more": false})
		return
	}
	if len(parts) == 3 && r.Method == http.MethodPost {
		var input identity.Credentials
		if !decodeJSON(w, r, &input) {
			return
		}
		member, err := a.service.CreateMember(ctx, p, input)
		if err != nil {
			databaseError(w, err)
			return
		}
		writeJSON(w, 201, map[string]any{"user": member})
		return
	}
	if len(parts) != 4 {
		http.NotFound(w, r)
		return
	}
	id := parts[3]
	role := ""
	remove := r.Method == http.MethodDelete
	if r.Method == http.MethodPatch {
		var input struct {
			Role string `json:"role"`
		}
		if !decodeJSON(w, r, &input) {
			return
		}
		role = input.Role
	} else if !remove {
		methodNotAllowed(w)
		return
	}
	if err := a.service.ChangeMember(ctx, p, id, role, remove); err != nil {
		databaseError(w, err)
		return
	}
	writeJSON(w, 200, map[string]any{"user_id": id, "role": role, "deleted": remove})
}
