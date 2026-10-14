package identity

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/mail"
	"regexp"
	"strings"
	"time"

	"github.com/google/uuid"
)

var (
	ErrUnauthorized = errors.New("authentication required")
	ErrInvalid      = errors.New("invalid identity input")
	ErrForbidden    = errors.New("workspace access denied")
	ErrNotFound     = errors.New("identity resource not found")
	ErrConflict     = errors.New("identity resource conflict")
	ErrRetainOwner  = errors.New("workspace must retain an owner")
	ErrRemoveSelf   = errors.New("cannot remove current user")
)

type Principal struct {
	UserID      string `json:"id"`
	WorkspaceID string `json:"workspace_id"`
	Username    string `json:"username"`
	Email       string `json:"email"`
	DisplayName string `json:"display_name"`
	Role        string `json:"role"`
}

func (p Principal) MarshalJSON() ([]byte, error) {
	type alias Principal
	return json.Marshal(struct {
		alias
		UserIDAlias   string `json:"user_id"`
		TenantIDAlias string `json:"tenant_id"`
	}{alias(p), p.UserID, p.WorkspaceID})
}

type principalKey struct{}

func FromContext(ctx context.Context) (Principal, bool) {
	p, ok := ctx.Value(principalKey{}).(Principal)
	return p, ok
}
func WithPrincipal(ctx context.Context, p Principal) context.Context {
	return context.WithValue(ctx, principalKey{}, p)
}

type Credentials struct {
	Username      string `json:"username"`
	Email         string `json:"email"`
	Password      string `json:"password"`
	DisplayName   string `json:"display_name"`
	WorkspaceName string `json:"workspace_name"`
	TenantName    string `json:"tenant_name"`
	Identifier    string `json:"identifier"`
	Role          string `json:"role"`
}
type Session struct {
	Principal Principal
	Token     string
}
type Workspace struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}
type AuditLog struct {
	ID           int64     `json:"id"`
	Action       string    `json:"action"`
	ResourceType string    `json:"resource_type"`
	ResourceID   string    `json:"resource_id"`
	CreatedAt    time.Time `json:"created_at"`
}

type Repository interface {
	Session(context.Context, string) (Principal, error)
	FindLogin(context.Context, string) (Principal, string, error)
	Register(context.Context, Principal, string, string, string, time.Time) error
	CreateSession(context.Context, Principal, string, time.Time) error
	Logout(context.Context, Principal, string) error
	Workspace(context.Context, Principal) (Workspace, error)
	UpdateWorkspace(context.Context, Principal, string) (Workspace, error)
	AuditLogs(context.Context, Principal) ([]AuditLog, error)
	Members(context.Context, Principal) ([]Principal, error)
	CreateMember(context.Context, Principal, Principal, string) error
	ChangeMember(context.Context, Principal, string, string, bool) error
}
type PasswordCodec interface {
	Hash(string) (string, error)
	Verify(string, string) bool
}
type Service struct {
	repository   Repository
	passwords    PasswordCodec
	sessionHours int
}

func NewService(repository Repository, passwords PasswordCodec, sessionHours int) *Service {
	if sessionHours < 1 {
		sessionHours = 24
	}
	return &Service{repository: repository, passwords: passwords, sessionHours: sessionHours}
}

func sessionHash(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:])
}

var usernamePattern = regexp.MustCompile(`^[A-Za-z0-9_.-]{3,80}$`)

func ValidUsername(value string) bool { return usernamePattern.MatchString(value) }
func ValidRole(value string) bool     { return value == "owner" || value == "editor" || value == "viewer" }

func (s *Service) Authenticate(ctx context.Context, token string) (Principal, error) {
	if token == "" || s.repository == nil {
		return Principal{}, ErrUnauthorized
	}
	return s.repository.Session(ctx, sessionHash(token))
}
func (s *Service) newSession(p Principal) (Session, string, time.Time, error) {
	token := make([]byte, 32)
	if _, err := rand.Read(token); err != nil {
		return Session{}, "", time.Time{}, err
	}
	value := hex.EncodeToString(token)
	return Session{Principal: p, Token: value}, sessionHash(value), time.Now().Add(time.Duration(s.sessionHours) * time.Hour), nil
}

func (s *Service) Register(ctx context.Context, input Credentials) (Session, error) {
	input.Username = strings.TrimSpace(input.Username)
	input.Email = strings.ToLower(strings.TrimSpace(input.Email))
	if !ValidUsername(input.Username) || len(input.Password) < 8 || len(input.Password) > 200 {
		return Session{}, ErrInvalid
	}
	if input.Email == "" {
		input.Email = strings.ToLower(input.Username) + "@local.invalid"
	}
	if _, err := mail.ParseAddress(input.Email); err != nil {
		return Session{}, ErrInvalid
	}
	hash, err := s.passwords.Hash(input.Password)
	if err != nil {
		return Session{}, ErrInvalid
	}
	if input.DisplayName == "" {
		input.DisplayName = input.Username
	}
	p := Principal{UserID: uuid.NewString(), WorkspaceID: uuid.NewString(), Username: input.Username, Email: input.Email, DisplayName: input.DisplayName, Role: "owner"}
	name := strings.TrimSpace(input.WorkspaceName)
	if name == "" {
		name = strings.TrimSpace(input.TenantName)
	}
	if name == "" {
		name = input.Username + " workspace"
	}
	session, tokenHash, expires, err := s.newSession(p)
	if err != nil {
		return Session{}, err
	}
	if s.repository == nil {
		return Session{}, errors.New("identity storage unavailable")
	}
	if err = s.repository.Register(ctx, p, hash, name, tokenHash, expires); err != nil {
		return Session{}, err
	}
	return session, nil
}
func (s *Service) Login(ctx context.Context, input Credentials) (Session, error) {
	identifier := input.Username
	if input.Identifier != "" {
		identifier = input.Identifier
	}
	if identifier == "" {
		identifier = input.Email
	}
	if s.repository == nil {
		return Session{}, ErrUnauthorized
	}
	p, hash, err := s.repository.FindLogin(ctx, identifier)
	if err != nil || !s.passwords.Verify(hash, input.Password) {
		return Session{}, ErrUnauthorized
	}
	session, tokenHash, expires, err := s.newSession(p)
	if err != nil {
		return Session{}, err
	}
	if err = s.repository.CreateSession(ctx, p, tokenHash, expires); err != nil {
		return Session{}, err
	}
	return session, nil
}
func (s *Service) Logout(ctx context.Context, p Principal, token string) error {
	return s.repository.Logout(ctx, p, sessionHash(token))
}
func (s *Service) Workspace(ctx context.Context, p Principal) (Workspace, error) {
	return s.repository.Workspace(ctx, p)
}
func (s *Service) UpdateWorkspace(ctx context.Context, p Principal, name string) (Workspace, error) {
	if p.Role != "owner" {
		return Workspace{}, ErrForbidden
	}
	if strings.TrimSpace(name) == "" {
		return Workspace{}, ErrInvalid
	}
	return s.repository.UpdateWorkspace(ctx, p, name)
}
func (s *Service) AuditLogs(ctx context.Context, p Principal) ([]AuditLog, error) {
	if p.Role != "owner" {
		return nil, ErrForbidden
	}
	return s.repository.AuditLogs(ctx, p)
}
func (s *Service) Members(ctx context.Context, p Principal) ([]Principal, error) {
	return s.repository.Members(ctx, p)
}
func (s *Service) CreateMember(ctx context.Context, p Principal, input Credentials) (Principal, error) {
	if p.Role != "owner" {
		return Principal{}, ErrForbidden
	}
	if input.Role == "" {
		input.Role = "viewer"
	}
	if !ValidRole(input.Role) || !ValidUsername(input.Username) || len(input.Password) < 8 || len(input.Password) > 200 {
		return Principal{}, ErrInvalid
	}
	if input.Email == "" {
		input.Email = strings.ToLower(input.Username) + "@local.invalid"
	}
	if _, err := mail.ParseAddress(input.Email); err != nil {
		return Principal{}, ErrInvalid
	}
	hash, err := s.passwords.Hash(input.Password)
	if err != nil {
		return Principal{}, ErrInvalid
	}
	member := Principal{UserID: uuid.NewString(), WorkspaceID: p.WorkspaceID, Username: input.Username, Email: strings.ToLower(input.Email), DisplayName: input.DisplayName, Role: input.Role}
	if err = s.repository.CreateMember(ctx, p, member, hash); err != nil {
		return Principal{}, err
	}
	return member, nil
}
func (s *Service) ChangeMember(ctx context.Context, p Principal, id, role string, remove bool) error {
	if p.Role != "owner" {
		return ErrForbidden
	}
	if !remove && !ValidRole(role) {
		return ErrInvalid
	}
	if remove && id == p.UserID {
		return ErrRemoveSelf
	}
	return s.repository.ChangeMember(ctx, p, id, role, remove)
}
