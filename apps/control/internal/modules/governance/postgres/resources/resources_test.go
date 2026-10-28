package resources

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/database"
)

func TestDecodeResourceWriteRejectsUnknownFields(t *testing.T) {
	var input resourceWrite
	if err := decodeResourceWrite(json.RawMessage(`{"key":"valid","name":"Valid","unexpected":true}`), &input); err == nil {
		t.Fatal("unknown resource fields must be rejected")
	}
}

func TestResourceRepositoryWithoutStorageReturnsError(t *testing.T) {
	repo := NewRepository(nil, nil, nil, nil, nil, nil)
	if _, _, err := repo.ExecuteResourceCatalogOp(context.Background(), Principal{}, "styles", nil, governance.Read, nil, nil); err == nil {
		t.Fatal("missing storage must return an error")
	}
}

func TestDecodeResourceWriteRejectsMissingBody(t *testing.T) {
	var input resourceWrite
	if err := decodeResourceWrite(nil, &input); err == nil {
		t.Fatal("missing request body must be rejected")
	}
}

func TestDecodeResourceWriteRejectsTrailingJSON(t *testing.T) {
	var input resourceWrite
	if err := decodeResourceWrite(json.RawMessage(`{"key":"valid","name":"Valid"} {}`), &input); err == nil {
		t.Fatal("trailing JSON values must be rejected")
	}
}

func TestDecodeResourceWriteAcceptsOneObject(t *testing.T) {
	var input resourceWrite
	if err := decodeResourceWrite(json.RawMessage(`{"key":"valid","name":"Valid"}`), &input); err != nil {
		t.Fatalf("valid resource body rejected: %v", err)
	}
	if input.Key != "valid" || input.Name != "Valid" {
		t.Fatalf("decoded unexpected resource body: %#v", input)
	}
}

func TestRepositoryResourceLifecycleAndWorkspaceIsolation(t *testing.T) {
	url := os.Getenv("MQ_TEST_CONTROL_DATABASE_URL")
	if url == "" {
		t.Skip("MQ_TEST_CONTROL_DATABASE_URL is not configured")
	}
	ctx := context.Background()
	pool, err := pgxpool.New(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(pool.Close)

	owner := Principal{WorkspaceID: uuid.NewString(), UserID: uuid.NewString()}
	other := Principal{WorkspaceID: uuid.NewString(), UserID: uuid.NewString()}
	for _, principal := range []Principal{owner, other} {
		createResourceTestPrincipal(t, ctx, pool, principal)
		principal := principal
		t.Cleanup(func() {
			tx, beginErr := database.BeginWorkspaceTx(context.Background(), pool, principal.WorkspaceID)
			if beginErr != nil {
				t.Errorf("begin cleanup transaction: %v", beginErr)
				return
			}
			defer tx.Rollback(context.Background())
			if _, deleteErr := tx.Exec(context.Background(), "DELETE FROM workspaces WHERE id=$1", principal.WorkspaceID); deleteErr != nil {
				t.Errorf("delete test workspace: %v", deleteErr)
				return
			}
			if commitErr := tx.Commit(context.Background()); commitErr != nil {
				t.Errorf("commit workspace cleanup: %v", commitErr)
			}
		})
	}

	repo := NewRepository(
		pool,
		func(ctx context.Context, tx pgx.Tx, principal Principal, action, resourceType, resourceID string) error {
			_, err := tx.Exec(ctx, "INSERT INTO audit_logs(workspace_id,actor_id,action,resource_type,resource_id) VALUES($1,$2,$3,$4,$5)", principal.WorkspaceID, principal.UserID, action, resourceType, resourceID)
			return err
		},
		governance.NormalizePayload,
		func(string, string) (ResourceVersion, bool) { return ResourceVersion{}, false },
		governance.RegexpResourceKey,
		map[string]bool{},
	)

	status, createdValue, err := repo.ExecuteResourceCatalogOp(ctx, owner, "styles", nil, governance.Create, nil, json.RawMessage(`{"key":"migration-probe","name":"Migration probe","payload":{"pacing":"brisk"}}`))
	if err != nil || status != 201 {
		t.Fatalf("create resource: status=%d err=%v", status, err)
	}
	created := createdValue.(ResourceVersion)

	status, publishedValue, err := repo.ExecuteResourceCatalogOp(ctx, owner, "styles", []string{created.ID, "publish"}, governance.Create, nil, json.RawMessage(`{"expected_version":1}`))
	if err != nil || status != 200 {
		t.Fatalf("publish resource: status=%d err=%v", status, err)
	}
	published := publishedValue.(ResourceVersion)

	status, _, err = repo.ExecuteResourceCatalogOp(ctx, owner, "styles", []string{created.ID}, governance.Update, nil, json.RawMessage(`{"expected_version":2,"payload":{"pacing":"slow"}}`))
	if err != nil || status != 200 {
		t.Fatalf("update resource: status=%d err=%v", status, err)
	}

	frozen, err := repo.FrozenResource(ctx, owner.WorkspaceID, published.VersionID)
	if err != nil || frozen.Status != "published" || !strings.Contains(string(frozen.Payload), "brisk") {
		t.Fatalf("published version changed: %#v err=%v", frozen, err)
	}
	if _, err = repo.FrozenResource(ctx, other.WorkspaceID, published.VersionID); err == nil {
		t.Fatal("resource version was visible to another workspace")
	}
	if status, _, err = repo.ExecuteResourceCatalogOp(ctx, other, "styles", []string{created.ID}, governance.Read, nil, nil); err == nil || status != 0 {
		t.Fatalf("resource was visible to another workspace: status=%d err=%v", status, err)
	}
}

func createResourceTestPrincipal(t *testing.T, ctx context.Context, pool *pgxpool.Pool, principal Principal) {
	t.Helper()
	tx, err := database.BeginWorkspaceTx(ctx, pool, principal.WorkspaceID)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	if _, err = tx.Exec(ctx, "INSERT INTO workspaces(id,name) VALUES($1,$2)", principal.WorkspaceID, "governance resource test"); err != nil {
		t.Fatal(err)
	}
	if _, err = tx.Exec(ctx, "INSERT INTO users(id,workspace_id,username,email,role) VALUES($1,$2,$3,$4,'owner')", principal.UserID, principal.WorkspaceID, "test_"+strings.ReplaceAll(principal.UserID, "-", ""), fmt.Sprintf("%s@example.test", principal.UserID)); err != nil {
		t.Fatal(err)
	}
	if err = tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
}
