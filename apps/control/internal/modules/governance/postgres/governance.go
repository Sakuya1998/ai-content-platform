package postgres

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/sakuya/ai-content-platform/apps/control/internal/modules/governance"
)

type ResourceVersion = governance.ResourceVersion

const resourceSelect = `SELECT r.id::text,v.id::text,r.workspace_id::text,r.kind,r.key,v.version,v.name,v.description,v.payload,v.status,v.content_hash,v.created_at FROM governance_resources r JOIN governance_resource_versions v ON v.resource_id=r.id `

func scanResource(row pgx.Row) (ResourceVersion, error) {
	var v ResourceVersion
	err := row.Scan(&v.ID, &v.VersionID, &v.WorkspaceID, &v.Kind, &v.Key, &v.Version, &v.Name, &v.Description, &v.Payload, &v.Status, &v.ContentHash, &v.CreatedAt)
	return v, err
}

func (g *GovernanceRepository) FrozenResource(ctx context.Context, workspace, id string) (ResourceVersion, error) {
	tx, err := workspaceTx(ctx, g.pool, workspace)
	if err != nil {
		return ResourceVersion{}, err
	}
	defer tx.Rollback(ctx)
	return scanResource(tx.QueryRow(ctx, resourceSelect+"WHERE r.workspace_id=$1 AND v.id::text=$2", workspace, id))
}

type resourceWrite struct {
	Key             string          `json:"key"`
	Name            string          `json:"name"`
	Description     *string         `json:"description"`
	Payload         json.RawMessage `json:"payload"`
	ExpectedVersion int             `json:"expected_version"`
}

func (g *GovernanceRepository) executeResource(w *catalogResult, r *catalogRequest, p Principal, kind string, parts []string) {
	ctx := r.Context()
	tx, err := workspaceTx(ctx, g.pool, p.WorkspaceID)
	if err != nil {
		catalogDBError(w, err)
		return
	}
	defer tx.Rollback(ctx)
	if len(parts) == 0 && r.action == governance.Read {
		query := resourceSelect + "WHERE r.workspace_id=$1 AND r.kind=$2 AND v.version=r.current_version"
		args := []any{p.WorkspaceID, kind}
		if status := r.filters["status"]; status != "" {
			query += " AND v.status=$3"
			args = append(args, status)
		}
		rows, e := tx.Query(ctx, query+" ORDER BY r.key", args...)
		if e != nil {
			catalogDBError(w, e)
			return
		}
		defer rows.Close()
		items := []ResourceVersion{}
		for rows.Next() {
			item, e := scanResource(rows)
			if e != nil {
				catalogDBError(w, e)
				return
			}
			items = append(items, item)
		}
		if e = rows.Err(); e != nil {
			catalogDBError(w, e)
			return
		}
		if kind == "styles" && (r.filters["status"] == "" || r.filters["status"] == "published") {
			for _, key := range []string{"jin_yong", "gu_long", "murakami", "yu_hua"} {
				v, _ := builtinGovernanceStyle(p.WorkspaceID, key)
				items = append(items, v)
			}
		}
		catalogComplete(w, 200, map[string]any{"items": items, "has_more": false})
		return
	}
	var current ResourceVersion
	id := ""
	action := ""
	statusCode := 200
	if len(parts) > 0 {
		id = parts[0]
		builtin := false
		if kind == "styles" && strings.HasPrefix(id, "system_style_") {
			current, builtin = builtinGovernanceStyle(p.WorkspaceID, id)
			if !builtin {
				catalogReject(w, 404, "style not found")
				return
			}
			if version := r.filters["version"]; version != "" && version != "1" {
				catalogReject(w, 404, "version not found")
				return
			}
			if r.action == governance.Read {
				if len(parts) == 2 && parts[1] == "versions" {
					catalogComplete(w, 200, map[string]any{"items": []ResourceVersion{current}})
				} else if len(parts) == 1 {
					catalogComplete(w, 200, current)
				} else {
					catalogNotFound(w)
				}
				return
			}
			if len(parts) != 2 || (parts[1] != "clone" && parts[1] != "copy") {
				catalogReject(w, 409, "built-in resources must be cloned before editing")
				return
			}
			action = parts[1]
		}
		if !builtin {
			// Serialize all edits and transitions of one resource.
			err = tx.QueryRow(ctx, "SELECT id::text FROM governance_resources WHERE workspace_id=$1 AND kind=$2 AND id::text=$3 FOR UPDATE", p.WorkspaceID, kind, id).Scan(&id)
			if err != nil {
				catalogDBError(w, err)
				return
			}
			if len(parts) == 2 {
				action = parts[1]
			}
			if len(parts) > 2 {
				catalogNotFound(w)
				return
			}
			if action == "versions" && r.action == governance.Read {
				rows, e := tx.Query(ctx, resourceSelect+"WHERE r.workspace_id=$1 AND r.kind=$2 AND r.id=$3 ORDER BY v.version DESC", p.WorkspaceID, kind, id)
				if e != nil {
					catalogDBError(w, e)
					return
				}
				defer rows.Close()
				items := []ResourceVersion{}
				for rows.Next() {
					v, e := scanResource(rows)
					if e != nil {
						catalogDBError(w, e)
						return
					}
					items = append(items, v)
				}
				if e = rows.Err(); e != nil {
					catalogDBError(w, e)
					return
				}
				catalogComplete(w, 200, map[string]any{"items": items})
				return
			}
			query := resourceSelect + "WHERE r.workspace_id=$1 AND r.kind=$2 AND r.id=$3 AND v.version=r.current_version"
			args := []any{p.WorkspaceID, kind, id}
			if version := r.filters["version"]; version != "" && r.action == governance.Read {
				query = resourceSelect + "WHERE r.workspace_id=$1 AND r.kind=$2 AND r.id=$3 AND v.version::text=$4"
				args = append(args, version)
			}
			current, err = scanResource(tx.QueryRow(ctx, query, args...))
			if err != nil {
				catalogDBError(w, err)
				return
			}
			if r.action == governance.Read && action == "" {
				catalogComplete(w, 200, current)
				return
			}
		}
	}
	var input resourceWrite
	if !decodeCatalogJSON(w, r, &input) {
		return
	}
	if input.ExpectedVersion > 0 && input.ExpectedVersion != current.Version {
		catalogReject(w, 409, "resource version changed")
		return
	}
	next := current
	next.Version++
	next.Status = "draft"
	next.VersionID = uuid.NewString()
	if len(parts) == 0 && r.action == governance.Create || action == "clone" || action == "copy" {
		if r.action != governance.Create {
			catalogUnsupported(w)
			return
		}
		if !regexpResourceKey(input.Key) {
			catalogReject(w, 422, "invalid resource key")
			return
		}
		if kind == "styles" {
			if _, reserved := governanceBuiltinStyles[input.Key]; reserved {
				catalogReject(w, 409, "built-in style key is reserved")
				return
			}
		}
		if strings.TrimSpace(input.Name) == "" {
			catalogReject(w, 422, "name required")
			return
		}
		id = uuid.NewString()
		next.ID = id
		next.Version = 1
		next.Key = input.Key
		next.Name = input.Name
		next.WorkspaceID = p.WorkspaceID
		next.Kind = kind
		_, err = tx.Exec(ctx, "INSERT INTO governance_resources(id,workspace_id,kind,key) VALUES($1,$2,$3,$4)", id, p.WorkspaceID, kind, input.Key)
		statusCode = 201
	} else if action == "publish" || action == "disable" {
		if r.action != governance.Create {
			catalogUnsupported(w)
			return
		}
		if action == "publish" {
			next.Status = "published"
		} else {
			next.Status = "disabled"
		}
	} else if action == "" && (r.action == governance.Update || r.action == governance.Replace) {
		if input.Key != "" && input.Key != current.Key {
			catalogReject(w, 422, "resource key is immutable; clone to change it")
			return
		}
		if input.Name != "" {
			next.Name = input.Name
		}
	} else {
		catalogUnsupported(w)
		return
	}
	if input.Description != nil {
		next.Description = *input.Description
	}
	if len(input.Payload) > 0 {
		next.Payload = input.Payload
	}
	if len(next.Payload) == 0 {
		next.Payload = json.RawMessage(`{}`)
	}
	if len(input.Payload) > 0 || current.ID == "" {
		if normalized, e := normalizeGovernancePayload(kind, next.Payload); e != nil {
			catalogReject(w, 422, e.Error())
			return
		} else {
			next.Payload = normalized
		}
	}
	if kind == "content_types" {
		var payload map[string]any
		_ = json.Unmarshal(next.Payload, &payload)
		if parent, ok := payload["parent_id"].(string); ok && parent != "" {
			var exists bool
			err = tx.QueryRow(ctx, "SELECT EXISTS(SELECT 1 FROM governance_resources WHERE workspace_id=$1 AND kind='content_types' AND id::text=$2 AND id<>$3)", p.WorkspaceID, parent, id).Scan(&exists)
			if err != nil {
				catalogDBError(w, err)
				return
			}
			if !exists {
				catalogReject(w, 422, "parent content type not found")
				return
			}
		}
	}
	digest := sha256.Sum256(next.Payload)
	next.ContentHash = hex.EncodeToString(digest[:])
	next.CreatedAt = time.Now().UTC()
	if err == nil {
		_, err = tx.Exec(ctx, "INSERT INTO governance_resource_versions(id,workspace_id,resource_id,version,name,description,payload,status,content_hash,created_by) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)", next.VersionID, p.WorkspaceID, id, next.Version, next.Name, next.Description, next.Payload, next.Status, next.ContentHash, p.UserID)
	}
	if err == nil {
		_, err = tx.Exec(ctx, "UPDATE governance_resources SET current_version=$3 WHERE workspace_id=$1 AND id=$2", p.WorkspaceID, id, next.Version)
	}
	if err == nil {
		err = g.writeAudit(ctx, tx, p, "governance.resource."+next.Status, kind, id)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		catalogDBError(w, err)
		return
	}
	catalogComplete(w, statusCode, next)
}

const modelProfileSelect = `SELECT p.id::text,v.id::text,v.version,v.name,v.provider,v.base_url,v.encrypted_key,v.chat_models,v.embedding_models,v.max_tokens,v.video_models,v.image_models,v.speech_models,v.music_models,v.sound_effect_models FROM governance_model_profiles p JOIN governance_model_profile_versions v ON v.profile_id=p.id `

func scanModelProfile(row pgx.Row) (ModelProfile, error) {
	var p ModelProfile
	var chat, embedding, video, image, speech, music, soundEffects []byte
	err := row.Scan(&p.ID, &p.VersionID, &p.Version, &p.Name, &p.Provider, &p.BaseURL, &p.EncryptedKey, &chat, &embedding, &p.MaxTokens, &video, &image, &speech, &music, &soundEffects)
	if err == nil {
		err = json.Unmarshal(chat, &p.ChatModels)
	}
	if err == nil {
		err = json.Unmarshal(embedding, &p.EmbeddingModels)
	}
	if err == nil {
		err = json.Unmarshal(video, &p.VideoModels)
	}
	if err == nil {
		err = json.Unmarshal(image, &p.ImageModels)
	}
	if err == nil {
		err = json.Unmarshal(speech, &p.SpeechModels)
	}
	if err == nil {
		err = json.Unmarshal(music, &p.MusicModels)
	}
	if err == nil {
		err = json.Unmarshal(soundEffects, &p.SoundEffectModels)
	}
	p.HasAPIKey = len(p.EncryptedKey) > 0
	if p.HasAPIKey {
		p.APIKey = "********"
	}
	return p, err
}

func (g *GovernanceRepository) freezeTarget(ctx context.Context, tx pgx.Tx, workspace, kind string, target RouteTarget) (RouteTarget, error) {
	if target.ModelName == "" || target.ProfileID == "" {
		return target, errors.New("profile and model required")
	}
	p, err := scanModelProfile(tx.QueryRow(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND p.id::text=$2 AND NOT p.deleted AND v.version=p.current_version FOR SHARE OF p", workspace, target.ProfileID))
	if err != nil {
		return target, err
	}
	models := p.ChatModels
	if kind == "embedding" {
		models = p.EmbeddingModels
	}
	if kind == "video_generation" {
		if _, ok := videoModelCapabilities(p.VideoModels, target.ModelName); !ok {
			return target, errors.New("video model is not in profile")
		}
	} else if kind == "image_generation" {
		if _, ok := imageModelCapabilities(p.ImageModels, target.ModelName); !ok {
			return target, errors.New("image model is not in profile")
		}
	} else if kind == "speech_synthesis" {
		if _, ok := speechModelCapabilities(p.SpeechModels, target.ModelName); !ok {
			return target, errors.New("speech model is not in profile")
		}
	} else if kind == "music_generation" {
		if _, ok := musicModelCapabilities(p.MusicModels, target.ModelName); !ok {
			return target, errors.New("music model is not in profile")
		}
	} else if kind == "sound_effect_generation" {
		if _, ok := soundEffectModelCapabilities(p.SoundEffectModels, target.ModelName); !ok {
			return target, errors.New("sound effect model is not in profile")
		}
	} else if !modelContains(models, target.ModelName) {
		return target, errors.New("model is not in profile")
	}
	target.ProfileVersionID = p.VersionID
	if (target.FallbackProfileID == "") != (target.FallbackModelName == "") {
		return target, errors.New("fallback profile and model required")
	}
	if target.FallbackProfileID != "" {
		target.Fallback = append(target.Fallback, RouteTarget{ProfileID: target.FallbackProfileID, ModelName: target.FallbackModelName})
		target.FallbackProfileID = ""
		target.FallbackModelName = ""
	}
	if len(target.Fallback) > 4 {
		return target, errors.New("too many fallback routes")
	}
	for i, fallback := range target.Fallback {
		if len(fallback.Fallback) > 0 || fallback.FallbackProfileID != "" {
			return target, errors.New("nested fallback not supported")
		}
		target.Fallback[i], err = g.freezeTarget(ctx, tx, workspace, kind, fallback)
		if err != nil {
			return target, err
		}
	}
	return target, nil
}

func (g *GovernanceRepository) resolveTarget(ctx context.Context, tx pgx.Tx, workspace, kind string, target RouteTarget) (FrozenRoute, error) {
	p, err := scanModelProfile(tx.QueryRow(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND v.id::text=$2", workspace, target.ProfileVersionID))
	if err != nil {
		return FrozenRoute{}, err
	}
	secret, err := g.decryptSecret(p.EncryptedKey)
	if err != nil {
		return FrozenRoute{}, err
	}
	base := p.BaseURL
	if base == "" {
		base, _ = providerURL(p.Provider)
	}
	route := FrozenRoute{ProfileVersionID: p.VersionID, Provider: p.Provider, ModelName: target.ModelName, BaseURL: base, APIKey: secret, MaxTokens: p.MaxTokens, Fallback: []FrozenRoute{}}
	if kind == "video_generation" {
		capabilities, ok := videoModelCapabilities(p.VideoModels, target.ModelName)
		if !ok {
			return route, errors.New("video model is not in frozen profile")
		}
		route.VideoCapabilities = &capabilities
	}
	if kind == "image_generation" {
		capabilities, ok := imageModelCapabilities(p.ImageModels, target.ModelName)
		if !ok {
			return route, errors.New("image model is not in frozen profile")
		}
		route.ImageCapabilities = &capabilities
	}
	if kind == "speech_synthesis" {
		capabilities, ok := speechModelCapabilities(p.SpeechModels, target.ModelName)
		if !ok {
			return route, errors.New("speech model is not in frozen profile")
		}
		route.SpeechCapabilities = &capabilities
	}
	if kind == "music_generation" {
		capabilities, ok := musicModelCapabilities(p.MusicModels, target.ModelName)
		if !ok {
			return route, errors.New("music model is not in frozen profile")
		}
		route.MusicCapabilities = &capabilities
	}
	if kind == "sound_effect_generation" {
		capabilities, ok := soundEffectModelCapabilities(p.SoundEffectModels, target.ModelName)
		if !ok {
			return route, errors.New("sound effect model is not in frozen profile")
		}
		route.SoundEffectCapabilities = &capabilities
	}
	for _, fallback := range target.Fallback {
		resolved, err := g.resolveTarget(ctx, tx, workspace, kind, fallback)
		if err != nil {
			return route, err
		}
		route.Fallback = append(route.Fallback, resolved)
	}
	return route, nil
}

func (g *GovernanceRepository) ResolveFrozenRoutes(ctx context.Context, workspace, id string) (FrozenRoutes, error) {
	tx, err := workspaceTx(ctx, g.pool, workspace)
	if err != nil {
		return FrozenRoutes{}, err
	}
	defer tx.Rollback(ctx)
	var data []byte
	result := FrozenRoutes{WorkspaceID: workspace, Routes: map[string]FrozenRoute{}}
	query := "SELECT id::text,version,routes FROM governance_model_routes WHERE workspace_id=$1 AND id::text=$2"
	args := []any{workspace, id}
	if id == "current" {
		query = "SELECT id::text,version,routes FROM governance_model_routes WHERE workspace_id=$1 ORDER BY version DESC LIMIT 1"
		args = []any{workspace}
	}
	if err = tx.QueryRow(ctx, query, args...).Scan(&result.ID, &result.Version, &data); err != nil {
		return result, err
	}
	var targets map[string]RouteTarget
	if err = json.Unmarshal(data, &targets); err != nil {
		return result, err
	}
	for kind, target := range targets {
		result.Routes[kind], err = g.resolveTarget(ctx, tx, workspace, kind, target)
		if err != nil {
			return result, err
		}
	}
	return result, nil
}

func (g *GovernanceRepository) CurrentRouteVersion(ctx context.Context, workspace string) (string, error) {
	tx, err := workspaceTx(ctx, g.pool, workspace)
	if err != nil {
		return "", err
	}
	defer tx.Rollback(ctx)
	var id string
	err = tx.QueryRow(ctx, "SELECT id::text FROM governance_model_routes WHERE workspace_id=$1 ORDER BY version DESC LIMIT 1", workspace).Scan(&id)
	return id, err
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
		v, e := scanResource(tx.QueryRow(ctx, resourceSelect+"WHERE r.workspace_id=$1 AND v.id::text=$2 AND v.status='published'", workspace, id))
		if e != nil {
			return nil, e
		}
		resources = append(resources, v)
		switch v.Kind {
		case "content_types":
			snapshot["content_type_snapshot"] = v
		case "styles":
			snapshot["style_snapshot"] = v
		case "creative_templates":
			snapshot["creative_template_snapshot"] = v
		case "quality_policies":
			snapshot["quality_policy_snapshot"] = v
		default:
			capabilities = append(capabilities, v)
		}
	}
	snapshot["resources"] = resources
	snapshot["capabilities"] = capabilities
	raw, err := json.Marshal(snapshot)
	return raw, err
}

func (g *GovernanceRepository) executeModel(w *catalogResult, r *catalogRequest, p Principal, path string) {
	ctx := r.Context()
	tx, err := workspaceTx(ctx, g.pool, p.WorkspaceID)
	if err != nil {
		catalogDBError(w, err)
		return
	}
	defer tx.Rollback(ctx)
	if (path == "" || path == "/profiles") && r.action == governance.Read {
		rows, e := tx.Query(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND NOT p.deleted AND v.version=p.current_version ORDER BY v.name", p.WorkspaceID)
		if e != nil {
			catalogDBError(w, e)
			return
		}
		profiles := []ModelProfile{}
		for rows.Next() {
			profile, e := scanModelProfile(rows)
			if e != nil {
				rows.Close()
				catalogDBError(w, e)
				return
			}
			profiles = append(profiles, profile)
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			catalogDBError(w, e)
			return
		}
		var id string
		var version int
		var routes json.RawMessage
		err = tx.QueryRow(ctx, "SELECT id::text,version,routes FROM governance_model_routes WHERE workspace_id=$1 ORDER BY version DESC LIMIT 1", p.WorkspaceID).Scan(&id, &version, &routes)
		if errors.Is(err, pgx.ErrNoRows) {
			routes = json.RawMessage(`{}`)
			err = nil
		}
		if err != nil {
			catalogDBError(w, err)
			return
		}
		catalogComplete(w, 200, map[string]any{"profiles": profiles, "routes": routes, "route_version_id": id, "route_version": version})
		return
	}
	if path == "/routes" && (r.action == governance.Replace || r.action == governance.Create) {
		var targets map[string]RouteTarget
		if !decodeCatalogJSON(w, r, &targets) {
			return
		}
		for _, kind := range []string{"creative", "analysis", "embedding"} {
			_, ok := targets[kind]
			if !ok {
				catalogReject(w, 422, "creative, analysis, embedding routes required")
				return
			}
		}
		for kind, target := range targets {
			if !validRouteKind(kind) {
				catalogReject(w, 422, "unknown route kind")
				return
			}
			target, e := g.freezeTarget(ctx, tx, p.WorkspaceID, kind, target)
			if e != nil {
				catalogReject(w, 422, "invalid model route")
				return
			}
			targets[kind] = target
		}
		if _, err = tx.Exec(ctx, "SELECT id FROM workspaces WHERE id=$1 FOR UPDATE", p.WorkspaceID); err != nil {
			catalogDBError(w, err)
			return
		}
		var version int
		err = tx.QueryRow(ctx, "SELECT coalesce(max(version),0)+1 FROM governance_model_routes WHERE workspace_id=$1", p.WorkspaceID).Scan(&version)
		id := uuid.NewString()
		data, _ := json.Marshal(targets)
		if err == nil {
			_, err = tx.Exec(ctx, "INSERT INTO governance_model_routes(id,workspace_id,version,routes,created_by) VALUES($1,$2,$3,$4,$5)", id, p.WorkspaceID, version, data, p.UserID)
		}
		if err == nil {
			err = g.writeAudit(ctx, tx, p, "governance.model_routes.updated", "model_routes", id)
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			catalogDBError(w, err)
			return
		}
		catalogComplete(w, 200, map[string]any{"route_version_id": id, "version": version, "routes": targets})
		return
	}
	if !strings.HasPrefix(path, "/profiles") {
		catalogNotFound(w)
		return
	}
	parts := strings.Split(strings.TrimPrefix(path, "/profiles"), "/")
	id := ""
	current := ModelProfile{}
	if len(parts) > 1 && parts[1] != "" {
		id = parts[1]
		err = tx.QueryRow(ctx, "SELECT id::text FROM governance_model_profiles WHERE workspace_id=$1 AND id::text=$2 AND NOT deleted FOR UPDATE", p.WorkspaceID, id).Scan(&id)
		if err == nil {
			current, err = scanModelProfile(tx.QueryRow(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND p.id=$2 AND v.version=p.current_version", p.WorkspaceID, id))
		}
		if err != nil {
			catalogDBError(w, err)
			return
		}
	}
	if len(parts) == 3 && parts[2] == "test" && r.action == governance.Create {
		g.testModel(w, r, current)
		return
	}
	if len(parts) > 2 {
		catalogNotFound(w)
		return
	}
	if r.action == governance.Read && id != "" {
		catalogComplete(w, 200, current)
		return
	}
	if r.action == governance.Delete && id != "" {
		var data []byte
		err = tx.QueryRow(ctx, "SELECT routes FROM governance_model_routes WHERE workspace_id=$1 ORDER BY version DESC LIMIT 1", p.WorkspaceID).Scan(&data)
		if err != nil && !errors.Is(err, pgx.ErrNoRows) {
			catalogDBError(w, err)
			return
		}
		var targets map[string]RouteTarget
		_ = json.Unmarshal(data, &targets)
		for _, target := range targets {
			if routeUsesProfile(target, id) {
				catalogReject(w, 409, "profile is used by current routes")
				return
			}
		}
		_, err = tx.Exec(ctx, "UPDATE governance_model_profiles SET deleted=true WHERE workspace_id=$1 AND id=$2", p.WorkspaceID, id)
		if err == nil {
			err = g.writeAudit(ctx, tx, p, "governance.model_profile.deleted", "model_profile", id)
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			catalogDBError(w, err)
			return
		}
		catalogComplete(w, 200, map[string]any{"deleted": true, "profile_id": id})
		return
	}
	if !(r.action == governance.Create && id == "" || (r.action == governance.Replace || r.action == governance.Update) && id != "") {
		catalogUnsupported(w)
		return
	}
	var input modelProfileWrite
	if !decodeCatalogJSON(w, r, &input) {
		return
	}
	if input.VideoModels == nil {
		input.VideoModels = current.VideoModels
	}
	if input.ImageModels == nil {
		input.ImageModels = current.ImageModels
	}
	if input.SpeechModels == nil {
		input.SpeechModels = current.SpeechModels
	}
	if input.MusicModels == nil {
		input.MusicModels = current.MusicModels
	}
	if input.SoundEffectModels == nil {
		input.SoundEffectModels = current.SoundEffectModels
	}
	if e := validateModelProfile(input); e != nil {
		catalogReject(w, 422, e.Error())
		return
	}
	if input.MaxTokens <= 0 {
		input.MaxTokens = 4096
	}
	if input.ChatModels == nil {
		input.ChatModels = []string{}
	}
	if input.EmbeddingModels == nil {
		input.EmbeddingModels = []string{}
	}
	if input.VideoModels == nil {
		input.VideoModels = []VideoModel{}
	}
	if input.ImageModels == nil {
		input.ImageModels = []ImageModel{}
	}
	if input.SpeechModels == nil {
		input.SpeechModels = []SpeechModel{}
	}
	if input.MusicModels == nil {
		input.MusicModels = []MusicModel{}
	}
	if input.SoundEffectModels == nil {
		input.SoundEffectModels = []SoundEffectModel{}
	}
	encrypted := current.EncryptedKey
	if encrypted == nil {
		encrypted = []byte{}
	}
	if input.ClearAPIKey {
		encrypted = []byte{}
	} else if input.APIKey != "" && input.APIKey != "********" {
		encrypted, err = g.encryptSecret(input.APIKey)
		if err != nil {
			catalogReject(w, 503, "model key encryption is not configured")
			return
		}
	}
	status := 200
	version := current.Version + 1
	if id == "" {
		id = uuid.NewString()
		status = 201
		_, err = tx.Exec(ctx, "INSERT INTO governance_model_profiles(id,workspace_id) VALUES($1,$2)", id, p.WorkspaceID)
	}
	versionID := uuid.NewString()
	chat, _ := json.Marshal(input.ChatModels)
	embedding, _ := json.Marshal(input.EmbeddingModels)
	video, _ := json.Marshal(input.VideoModels)
	image, _ := json.Marshal(input.ImageModels)
	speech, _ := json.Marshal(input.SpeechModels)
	music, _ := json.Marshal(input.MusicModels)
	soundEffects, _ := json.Marshal(input.SoundEffectModels)
	if err == nil {
		_, err = tx.Exec(ctx, "INSERT INTO governance_model_profile_versions(id,workspace_id,profile_id,version,name,provider,base_url,encrypted_key,chat_models,embedding_models,max_tokens,video_models,image_models,speech_models,music_models,sound_effect_models) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)", versionID, p.WorkspaceID, id, version, input.Name, input.Provider, input.BaseURL, encrypted, chat, embedding, input.MaxTokens, video, image, speech, music, soundEffects)
	}
	if err == nil {
		_, err = tx.Exec(ctx, "UPDATE governance_model_profiles SET current_version=$3 WHERE workspace_id=$1 AND id=$2", p.WorkspaceID, id, version)
	}
	if err == nil {
		err = g.writeAudit(ctx, tx, p, "governance.model_profile.updated", "model_profile", id)
	}
	if err == nil {
		err = tx.Commit(ctx)
	}
	if err != nil {
		catalogDBError(w, err)
		return
	}
	profile := ModelProfile{ID: id, VersionID: versionID, Version: version, Name: input.Name, Provider: input.Provider, BaseURL: input.BaseURL, HasAPIKey: len(encrypted) > 0, ChatModels: input.ChatModels, EmbeddingModels: input.EmbeddingModels, VideoModels: input.VideoModels, ImageModels: input.ImageModels, SpeechModels: input.SpeechModels, MusicModels: input.MusicModels, SoundEffectModels: input.SoundEffectModels, MaxTokens: input.MaxTokens}
	if profile.HasAPIKey {
		profile.APIKey = "********"
	}
	catalogComplete(w, status, profile)
}

func (g *GovernanceRepository) ResolveFrozenRoute(ctx context.Context, workspace, versionID, kind string) (FrozenRoute, error) {
	routes, err := g.ResolveFrozenRoutes(ctx, workspace, versionID)
	if err != nil {
		return FrozenRoute{}, err
	}
	route, ok := routes.Routes[kind]
	if !ok {
		return route, fmt.Errorf("route kind %s not configured", kind)
	}
	return route, nil
}
