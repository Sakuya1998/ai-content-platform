package postgres

import (
	"context"
	"encoding/json"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/governance"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/identity"
	identitypostgres "github.com/sakuya/novel-agent/go-control-plane/internal/modules/identity/postgres"
	"github.com/sakuya/novel-agent/go-control-plane/internal/modules/workflow"
	"os"
	"testing"
)

func TestWorkflowVersionPublishingAndTenantIsolation(t *testing.T) {
	url := os.Getenv("MQ_TEST_CONTROL_DATABASE_URL")
	if url == "" {
		t.Skip("MQ_TEST_CONTROL_DATABASE_URL is not configured")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	defer pool.Close()
	workspace, user := uuid.NewString(), uuid.NewString()
	if _, err = pool.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,'Workflow test')", workspace); err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, "INSERT INTO users(id,workspace_id,username,email,display_name,role) VALUES($1,$2,$3,$4,'Owner','owner')", user, workspace, "workflow_"+user, user+"@test.invalid"); err != nil {
		t.Fatal(err)
	}
	p := identity.Principal{UserID: user, WorkspaceID: workspace, Role: "owner"}
	repo := NewRepository(pool, identitypostgres.RecordPrincipalAudit)
	service := workflow.NewService(repo)
	nodes, _ := governance.WorkflowPromptNodes("content.generate@1")
	definition := workflow.Definition{Template: "content.generate@1", Nodes: nodes}
	first, err := service.Create(ctx, p, "Project generation", definition)
	if err != nil {
		t.Fatal(err)
	}
	if first.Status != "draft" || first.Version != 1 {
		t.Fatalf("new version: %#v", first)
	}
	if _, err = service.Publish(ctx, identity.Principal{WorkspaceID: workspace, Role: "viewer"}, first.ID); err == nil {
		t.Fatal("viewer published")
	}
	first, err = service.Publish(ctx, p, first.ID)
	if err != nil {
		t.Fatal(err)
	}
	frozen, err := service.Frozen(ctx, workspace, definition.Template)
	if err != nil || frozen.ID != first.ID {
		t.Fatalf("frozen definition: %v", err)
	}
	second, err := service.Create(ctx, p, "Project generation", definition)
	if err != nil || second.Version != 2 {
		t.Fatalf("next version: %v", err)
	}
	if _, err = service.Get(ctx, identity.Principal{WorkspaceID: uuid.NewString()}, first.ID); err == nil {
		t.Fatal("cross-tenant version visible")
	}
	tx, err := pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, "SELECT set_config('app.workspace_id',$1,true)", workspace); err != nil {
		t.Fatal(err)
	}
	data, _ := json.Marshal(workflow.Definition{Template: "python.exec@1", Nodes: []string{"exec"}})
	if _, err = tx.Exec(ctx, "UPDATE workflow_templates SET definition=$2 WHERE id=$1", first.ID, data); err == nil {
		t.Fatal("published version mutable")
	}
	_ = tx.Rollback(ctx)
	if _, err = service.Disable(ctx, p, first.ID); err != nil {
		t.Fatal(err)
	}
	if _, err = service.Publish(ctx, p, first.ID); err == nil {
		t.Fatal("disabled version republished")
	}
}
