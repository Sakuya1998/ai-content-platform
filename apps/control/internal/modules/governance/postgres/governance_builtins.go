package postgres

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"strings"

	"github.com/jackc/pgx/v5"
)

var governanceBuiltinStyles = map[string]struct {
	name    string
	payload string
}{
	"jin_yong": {"金庸风格", `{"syntax":["使用流畅的汉语语法","多用逗号连接的复合句","段落长短相间"],"sentence_length":"中等偏长,平均30-50字","vocabulary":["四字成语频繁使用","书面语为主","古典武侠术语"],"narrative_techniques":["全知视角","伏笔","侧面烘托","对比"],"pacing":"张弛有度,大场面与抒情交替","examples":["那少年长剑一挥,剑光如匹练般横贯长空,恍若银河倒泻。","他心中一凛,暗想:此人内力之深,实已臻化境,我万万不是对手。","夕阳西下,余晖洒满山谷,映得漫山红叶如火如荼,煞是壮观。"]}`},
	"gu_long":  {"古龙风格", `{"syntax":["短句为主","大量换行","少用复合句"],"sentence_length":"短促有力,平均10-20字","vocabulary":["简洁直白","口语化","偶尔诗化表达"],"narrative_techniques":["悬念","转折","留白"],"pacing":"快节奏,持续紧张","examples":["刀很快。刀光一闪。人已倒下。","夜很冷。风很冷。他的心更冷。","天涯远不远?不远。人就在天涯,天涯怎么会远?"]}`},
	"murakami": {"村上春树风格", `{"syntax":["使用流畅的日语式翻译腔","大量比喻","独特的观察视角"],"sentence_length":"中等,平均20-40字","vocabulary":["日常词汇","音乐/文学/食品专有名词","独特的比喻"],"narrative_techniques":["第一人称","超现实元素","细致的日常描写"],"pacing":"舒缓,注重氛围和心理","examples":["如同置身于深海般的孤独感,将我轻轻包裹。","她像秋日的午后一样,安静而透明。","不可思议的是,那旋律一旦响起,我便再也无法逃避自己的记忆。"]}`},
	"yu_hua":   {"余华风格", `{"syntax":["朴素直接的语法","少修辞","多用短句和白描"],"sentence_length":"简短,平均10-25字","vocabulary":["朴素","民间词汇","直白有力"],"narrative_techniques":["零度叙事","黑色幽默","残酷现实"],"pacing":"冷静克制,暗流涌动","examples":["他活着,像一头牲口那样活着。","那年冬天,我爷爷死了,死得很平静。","月光照在路上,像是撒满了盐。"]}`},
}

func builtinStyleKeys() map[string]bool {
	keys := make(map[string]bool, len(governanceBuiltinStyles))
	for key := range governanceBuiltinStyles {
		keys[key] = true
	}
	return keys
}

func builtinGovernanceStyle(workspace, id string) (ResourceVersion, bool) {
	key := strings.TrimPrefix(id, "system_style_")
	style, ok := governanceBuiltinStyles[key]
	if !ok {
		return ResourceVersion{}, false
	}
	payload, _ := normalizeGovernancePayload("styles", json.RawMessage(style.payload))
	hash := sha256.Sum256(payload)
	return ResourceVersion{ID: "system_style_" + key, VersionID: "system_style_" + key + "@1", WorkspaceID: workspace, Kind: "styles", Key: key, Version: 1, Name: style.name, Description: "Built-in style", Payload: payload, Status: "published", ContentHash: hex.EncodeToString(hash[:])}, true
}

func (g *GovernanceRepository) FreezeContentInput(ctx context.Context, workspace string, input map[string]any) (map[string]any, error) {
	tx, err := workspaceTx(ctx, g.pool, workspace)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	ids := []string{}
	builtins := []ResourceVersion{}
	for field, kind := range map[string]string{"primary_type_id": "content_types", "style_resource_id": "styles", "creative_template_id": "creative_templates", "quality_policy_id": "quality_policies"} {
		id, _ := input[field].(string)
		if id == "" {
			continue
		}
		if kind == "styles" {
			if v, ok := builtinGovernanceStyle(workspace, id); ok {
				builtins = append(builtins, v)
				continue
			}
		}
		v, e := scanResource(tx.QueryRow(ctx, resourceSelect+"WHERE r.workspace_id=$1 AND r.kind=$2 AND r.id::text=$3 AND v.version=r.current_version AND v.status='published'", workspace, kind, id))
		if e != nil {
			return nil, e
		}
		ids = append(ids, v.VersionID)
	}
	rows, err := tx.Query(ctx, `SELECT v.id::text FROM governance_resources r JOIN governance_resource_versions v ON v.resource_id=r.id AND v.version=r.current_version WHERE r.workspace_id=$1 AND r.kind='skill' AND v.status='published' ORDER BY r.kind,r.key`, workspace)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var id string
		if err = rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	frozen, err := g.FreezeSnapshot(ctx, workspace, ids)
	if err != nil {
		return nil, err
	}
	snapshot := map[string]any{}
	if err = json.Unmarshal(frozen, &snapshot); err != nil {
		return nil, err
	}
	for _, v := range builtins {
		snapshot["style_snapshot"] = v
	}
	if _, ok := snapshot["style_snapshot"]; !ok {
		style, _ := input["style"].(string)
		if style == "" {
			style = "jin_yong"
		}
		v, ok := builtinGovernanceStyle(workspace, style)
		if !ok {
			v, _ = builtinGovernanceStyle(workspace, "jin_yong")
		}
		snapshot["style_snapshot"] = v
	}
	for kind, field := range map[string]string{"content_types": "content_type_snapshot", "creative_templates": "creative_template_snapshot", "quality_policies": "quality_policy_snapshot"} {
		if _, ok := snapshot[field]; ok {
			continue
		}
		payload := json.RawMessage(`{}`)
		if kind == "creative_templates" {
			if brief, ok := input["brief"]; ok {
				payload, _ = json.Marshal(brief)
			}
		}
		payload, e := normalizeGovernancePayload(kind, payload)
		if e != nil {
			return nil, e
		}
		hash := sha256.Sum256(payload)
		snapshot[field] = ResourceVersion{ID: "system_" + kind + "_default", VersionID: "system_" + kind + "_default@1", WorkspaceID: workspace, Kind: kind, Key: "default", Name: "Default", Version: 1, Status: "published", Payload: payload, ContentHash: hex.EncodeToString(hash[:])}
	}
	result := map[string]any{}
	for _, field := range []string{"content_type_snapshot", "style_snapshot", "creative_template_snapshot", "quality_policy_snapshot"} {
		result[field] = snapshot[field]
	}
	return result, nil
}

func (g *GovernanceRepository) FreezeCapabilitySnapshot(ctx context.Context, workspace string, base map[string]any) (map[string]any, error) {
	tx, err := workspaceTx(ctx, g.pool, workspace)
	if err != nil {
		return nil, err
	}
	defer tx.Rollback(ctx)
	var routeID string
	var routeData []byte
	if err = tx.QueryRow(ctx, "SELECT id::text,routes FROM governance_model_routes WHERE workspace_id=$1 ORDER BY version DESC LIMIT 1", workspace).Scan(&routeID, &routeData); err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			return nil, errors.New("model routes must be configured before execution")
		}
		return nil, err
	}
	var available map[string]RouteTarget
	if err = json.Unmarshal(routeData, &available); err != nil {
		return nil, err
	}
	required := map[string]string{}
	switch routes := base["model_routes"].(type) {
	case map[string]string:
		for kind := range routes {
			required[kind] = routeID
		}
	case map[string]any:
		for kind := range routes {
			required[kind] = routeID
		}
	default:
		return nil, errors.New("snapshot model routes required")
	}
	if len(required) == 0 {
		// Historical novel snapshots leave the route aliases empty.
		required = map[string]string{"creative": routeID, "analysis": routeID, "embedding": routeID}
	}
	for kind := range required {
		if !validRouteKind(kind) {
			return nil, errors.New("unknown snapshot model route kind")
		}
		if _, ok := available[kind]; !ok {
			return nil, errors.New("required model route is not configured: " + kind)
		}
	}
	result := map[string]any{}
	for key, value := range base {
		result[key] = value
	}
	delete(result, "snapshot_hash")
	result["model_routes"] = required
	refs := map[string][]string{}
	for _, field := range []string{"skills", "tools", "mcp_policies"} {
		switch values := base[field].(type) {
		case []string:
			refs[field] = append([]string{}, values...)
		case []any:
			for _, value := range values {
				if s, ok := value.(string); ok {
					refs[field] = append(refs[field], s)
				}
			}
		}
		if refs[field] == nil {
			refs[field] = []string{}
		}
	}
	rows, err := tx.Query(ctx, `SELECT r.kind,v.id::text FROM governance_resources r JOIN governance_resource_versions v ON v.resource_id=r.id AND v.version=r.current_version WHERE r.workspace_id=$1 AND r.kind='skill' AND v.status='published' ORDER BY r.kind,r.key`, workspace)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var kind, id string
		if err = rows.Scan(&kind, &id); err != nil {
			return nil, err
		}
		field := map[string]string{"prompt_pack": "prompt_packs", "skill": "skills", "tool": "tools", "mcp_policy": "mcp_policies"}[kind]
		refs[field] = append(refs[field], id)
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	for field, values := range refs {
		result[field] = values
	}
	rows.Close()
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	if g.promptCatalog == nil {
		return nil, errors.New("prompt catalog must be configured before execution")
	}
	workflow, _ := base["workflow"].(string)
	binding, err := g.promptCatalog.Freeze(ctx, workspace, workflow)
	if err != nil {
		return nil, err
	}
	result["prompt_bindings"] = binding.Bindings
	result["prompt_packs"] = []string{binding.PackVersionID}
	if g.creationPolicies == nil {
		return nil, errors.New("creation policy catalog must be configured before execution")
	}
	policy, err := g.creationPolicies.Freeze(ctx, workspace, workflow)
	if err != nil {
		return nil, err
	}
	result["creation_policy"] = policy
	return result, nil
}
