package identity

import (
	"context"
	"testing"
	"time"
)

type memoryAuthentication struct {
	Repository
	principal    Principal
	passwordHash string
	sessionHash  string
	expires      time.Time
}

func (r *memoryAuthentication) Register(_ context.Context, p Principal, hash, name, session string, expires time.Time) error {
	r.principal, r.passwordHash, r.sessionHash, r.expires = p, hash, session, expires
	return nil
}
func (r *memoryAuthentication) FindLogin(context.Context, string) (Principal, string, error) {
	return r.principal, r.passwordHash, nil
}
func (r *memoryAuthentication) CreateSession(_ context.Context, p Principal, session string, expires time.Time) error {
	r.sessionHash, r.expires = session, expires
	return nil
}
func (r *memoryAuthentication) Session(_ context.Context, hash string) (Principal, error) {
	if hash != r.sessionHash {
		return Principal{}, ErrUnauthorized
	}
	return r.principal, nil
}

type testPasswords struct{}

func (testPasswords) Hash(password string) (string, error) { return "hash:" + password, nil }
func (testPasswords) Verify(hash, password string) bool    { return hash == "hash:"+password }

func TestRegistrationStoresOnlyHashedSessionAndAuthenticates(t *testing.T) {
	repo := &memoryAuthentication{}
	service := NewService(repo, testPasswords{}, 24)
	session, err := service.Register(context.Background(), Credentials{Username: "author", Password: "long-password"})
	if err != nil {
		t.Fatal(err)
	}
	if session.Token == "" || repo.sessionHash == session.Token || repo.passwordHash == "long-password" {
		t.Fatal("credentials were not hashed")
	}
	if session.Principal.Role != "owner" || session.Principal.Email != "author@local.invalid" {
		t.Fatal("registration defaults lost")
	}
	principal, err := service.Authenticate(context.Background(), session.Token)
	if err != nil || principal.UserID != session.Principal.UserID {
		t.Fatalf("authenticate: %v", err)
	}
	if _, err = service.Authenticate(context.Background(), "wrong"); err == nil {
		t.Fatal("unknown session accepted")
	}
}

func TestLoginRejectsIncorrectPasswordAndInvalidRegistration(t *testing.T) {
	repo := &memoryAuthentication{}
	service := NewService(repo, testPasswords{}, 24)
	if _, err := service.Register(context.Background(), Credentials{Username: "a", Password: "short"}); err != ErrInvalid {
		t.Fatalf("invalid registration: %v", err)
	}
	if _, err := service.Register(context.Background(), Credentials{Username: "author", Password: "long-password"}); err != nil {
		t.Fatal(err)
	}
	if _, err := service.Login(context.Background(), Credentials{Username: "author", Password: "wrong"}); err != ErrUnauthorized {
		t.Fatalf("invalid login: %v", err)
	}
}
