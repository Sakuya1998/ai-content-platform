package postgres

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	governancemodels "github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance/postgres/models"
	"github.com/jackc/pgx/v5"
)

type ResourceVersion = governance.ResourceVersion

const resourceSelect = `SELECT r.id::text,v.id::text,r.workspace_id::text,r.kind,r.key,v.version,v.name,v.description,v.payload,v.status,v.content_hash,v.created_at FROM governance_resources r JOIN governance_resource_versions v ON v.resource_id=r.id `

func scanResource(row pgx.Row) (ResourceVersion, error) {
	var version ResourceVersion
	err := row.Scan(&version.ID, &version.VersionID, &version.WorkspaceID, &version.Kind, &version.Key, &version.Version, &version.Name, &version.Description, &version.Payload, &version.Status, &version.ContentHash, &version.CreatedAt)
	return version, err
}

func (g *GovernanceRepository) FrozenResource(ctx context.Context, workspace, id string) (ResourceVersion, error) {
	return g.resourceCatalog.FrozenResource(ctx, workspace, id)
}

func (g *GovernanceRepository) ResolveFrozenRoutes(ctx context.Context, workspace, id string) (FrozenRoutes, error) {
	return g.modelCatalog.ResolveFrozenRoutes(ctx, workspace, id)
}

func (g *GovernanceRepository) ResolveFrozenRoute(ctx context.Context, workspace, versionID, kind string) (FrozenRoute, error) {
	return g.modelCatalog.ResolveFrozenRoute(ctx, workspace, versionID, kind)
}

func (g *GovernanceRepository) CurrentRouteVersion(ctx context.Context, workspace string) (string, error) {
	return g.modelCatalog.CurrentRouteVersion(ctx, workspace)
}

func (g *GovernanceRepository) FreezeSnapshot(ctx context.Context, workspace string, resourceVersionIDs []string) (json.RawMessage, error) {
	tx, err := workspaceTx(ctx, g.pool, workspace)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	snapshot := map[string]any{"schema_version": "1.0", "workspace_id": workspace, "resource_version_ids": resourceVersionIDs}
	var routeID string
	err = tx.QueryRow(ctx, "SELECT id::text FROM governance_model_routes WHERE workspace_id=$1 ORDER BY version DESC LIMIT 1", workspace).Scan(&routeID)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	if routeID != "" {
		snapshot["model_route_version_id"] = routeID
	}
	resources := []ResourceVersion{}
	capabilities := []ResourceVersion{}
	for _, id := range resourceVersionIDs {
		version, queryErr := scanResource(tx.QueryRow(ctx, resourceSelect+"WHERE r.workspace_id=$1 AND v.id::text=$2 AND v.status='published'", workspace, id))
		if queryErr != nil {
			return nil, queryErr
		}
		resources = append(resources, version)
		switch version.Kind {
		case "content_types":
			snapshot["content_type_snapshot"] = version
		case "styles":
			snapshot["style_snapshot"] = version
		case "creative_templates":
			snapshot["creative_template_snapshot"] = version
		case "quality_policies":
			snapshot["quality_policy_snapshot"] = version
		default:
			capabilities = append(capabilities, version)
		}
	}
	snapshot["resources"] = resources
	snapshot["capabilities"] = capabilities
	return json.Marshal(snapshot)
}

func FrozenModelProfile(ctx context.Context, tx pgx.Tx, workspace, id string) (governance.ModelProfile, error) {
	return governancemodels.FrozenModelProfile(ctx, tx, workspace, id)
}
