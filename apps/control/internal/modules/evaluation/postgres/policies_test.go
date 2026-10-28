package postgres

import (
	"context"
	"errors"
	"os"
	"testing"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/evaluation"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

func TestPolicyVersionsPublishArchiveAndRemainWorkspaceScoped(t *testing.T) {
	url := os.Getenv("MQ_TEST_CONTROL_DATABASE_URL")
	if url == "" {
		t.Skip("MQ_TEST_CONTROL_DATABASE_URL is not configured")
	}
	pool, err := pgxpool.New(context.Background(), url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)

	ctx := context.Background()
	workspace := uuid.NewString()
	if _, err = pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Evaluation policies')", workspace); err != nil {
		t.Fatal(err)
	}
	ctx = identity.WithPrincipal(ctx, identity.Principal{WorkspaceID: workspace, UserID: uuid.NewString(), Role: "owner"})
	repository := NewRepository(pool, func(context.Context, pgx.Tx, identity.Principal, string, string, string) error {
		return nil
	})
	for _, version := range []int{1, 2} {
		policy := &evaluation.EvaluationPolicyVersion{
			PolicyID: "content-quality", Version: version, EngineVersion: "evaluation@1",
			GateThreshold: 75, CriticalThreshold: 60, Status: evaluation.PolicyStatusDraft,
			Rules: []evaluation.EvaluationRule{{Key: "text.length@1", Dimension: "length", Weight: 1}},
		}
		if err = repository.CreatePolicy(ctx, workspace, policy.PolicyID, policy); err != nil {
			t.Fatalf("create version %d: %v", version, err)
		}
	}
	if err = repository.PublishPolicy(ctx, workspace, "content-quality", 1); err != nil {
		t.Fatalf("publish version 1: %v", err)
	}
	if err = repository.PublishPolicy(ctx, workspace, "content-quality", 2); err != nil {
		t.Fatalf("publish version 2: %v", err)
	}
	published, err := repository.GetPublishedPolicy(ctx, workspace, "content-quality")
	if err != nil || published.Version != 2 {
		t.Fatalf("published policy = %#v, %v", published, err)
	}
	archived, err := repository.GetPolicy(ctx, workspace, "content-quality", 1)
	if err != nil || archived.Status != evaluation.PolicyStatusArchived {
		t.Fatalf("prior version = %#v, %v", archived, err)
	}
	if err = repository.ArchivePolicy(ctx, workspace, "content-quality", 2); err != nil {
		t.Fatalf("archive version 2: %v", err)
	}
	if _, err = repository.GetPublishedPolicy(ctx, workspace, "content-quality"); !errors.Is(err, evaluation.ErrNotFound) {
		t.Fatalf("published policy error = %v, want ErrNotFound", err)
	}
	if _, err = repository.GetPolicy(ctx, uuid.NewString(), "content-quality", 2); !errors.Is(err, evaluation.ErrNotFound) {
		t.Fatalf("cross-workspace lookup error = %v, want ErrNotFound", err)
	}
}
