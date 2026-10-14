package app

import (
	"context"
	"io"
	"log/slog"
	"net"
	"net/http"
	"testing"
	"time"

	"github.com/sakuya/ai-content-platform/apps/control/internal/platform/config"
)

func TestRunWaitsForBackgroundTasksBeforeClosingResources(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	finished := make(chan struct{})
	closed := false
	application := &Application{
		logger:     slog.New(slog.NewJSONHandler(io.Discard, nil)),
		handler:    http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) }),
		background: []backgroundTask{{name: "test", run: func(ctx context.Context) { <-ctx.Done(); close(finished) }}},
		cleanup: []func(){func() {
			select {
			case <-finished:
				closed = true
			default:
				t.Error("resource closed before worker exit")
			}
		}},
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() { done <- application.Serve(ctx, listener) }()
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("shutdown timed out")
	}
	if !closed {
		t.Fatal("resources were not closed")
	}
}

func TestNewRequiresPersistentDatabaseUnlessExplicitlyAllowed(t *testing.T) {
	_, err := New(context.Background(), config.Config{}, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	if err == nil {
		t.Fatal("database requirement was bypassed")
	}
}

func TestMemoryApplicationServesHealthAndShutsDown(t *testing.T) {
	t.Setenv("CONTROL_PLANE_SECURITY_ALLOW_MEMORY", "true")
	cfg, err := config.Load()
	if err != nil {
		t.Fatal(err)
	}
	application, err := New(context.Background(), cfg, slog.New(slog.NewJSONHandler(io.Discard, nil)))
	if err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- application.Serve(ctx, listener) }()
	client := &http.Client{Timeout: 2 * time.Second}
	response, err := client.Get("http://" + listener.Addr().String() + "/healthz")
	if err != nil {
		cancel()
		<-done
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Errorf("health status: %d", response.StatusCode)
	}
	cancel()
	if err = <-done; err != nil {
		t.Fatal(err)
	}
}
