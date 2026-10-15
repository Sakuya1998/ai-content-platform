package prompts

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
)

// PromptMessage represents a single message in a prompt
type PromptMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// PromptVersion represents a versioned prompt resource
type PromptVersion struct {
	ID              string          `json:"id"`
	VersionID       string          `json:"version_id"`
	WorkspaceID     string          `json:"workspace_id,omitempty"`
	Key             string          `json:"key"`
	NodeKey         string          `json:"node_key"`
	Version         int             `json:"version"`
	Name            string          `json:"name"`
	Description     string          `json:"description"`
	Messages        []PromptMessage `json:"messages"`
	VariablesSchema json.RawMessage `json:"variables_schema"`
	OutputSchema    json.RawMessage `json:"output_schema"`
	RendererVersion string          `json:"renderer_version"`
	Status          string          `json:"status"`
	ContentHash     string          `json:"content_hash"`
	CreatedAt       time.Time       `json:"created_at"`
}

// PromptPackVersion represents a versioned prompt pack (collection of prompts)
type PromptPackVersion struct {
	ID          string          `json:"id"`
	VersionID   string          `json:"version_id"`
	WorkspaceID string          `json:"workspace_id,omitempty"`
	Key         string          `json:"key"`
	Version     int             `json:"version"`
	Name        string          `json:"name"`
	Description string          `json:"description"`
	Workflow    string          `json:"workflow"`
	Nodes       json.RawMessage `json:"nodes"`
	Status      string          `json:"status"`
	ContentHash string          `json:"content_hash"`
	CreatedAt   time.Time       `json:"created_at"`
}

const promptSelect = `SELECT p.id::text,v.id::text,p.workspace_id::text,p.key,v.node_key,v.version,v.name,v.description,v.messages,v.variables_schema,v.output_schema,v.renderer_version,v.status,v.content_hash,v.created_at FROM governance_prompts p JOIN governance_prompt_versions v ON v.prompt_id=p.id `

const packSelect = `SELECT p.id::text,v.id::text,p.workspace_id::text,p.key,v.version,v.name,v.description,v.workflow,v.nodes,v.status,v.content_hash,v.created_at FROM governance_prompt_packs p JOIN governance_prompt_pack_versions v ON v.pack_id=p.id `

// ScanPrompt reads a PromptVersion from a database row
func ScanPrompt(row pgx.Row) (PromptVersion, error) {
	var v PromptVersion
	var messages, variablesSchema, outputSchema []byte
	err := row.Scan(&v.ID, &v.VersionID, &v.WorkspaceID, &v.Key, &v.NodeKey, &v.Version, &v.Name, &v.Description, &messages, &variablesSchema, &outputSchema, &v.RendererVersion, &v.Status, &v.ContentHash, &v.CreatedAt)
	if err == nil {
		err = json.Unmarshal(messages, &v.Messages)
	}
	if err == nil && len(variablesSchema) > 0 {
		v.VariablesSchema = variablesSchema
	}
	if err == nil && len(outputSchema) > 0 {
		v.OutputSchema = outputSchema
	}
	return v, err
}

// ScanPromptPack reads a PromptPackVersion from a database row
func ScanPromptPack(row pgx.Row) (PromptPackVersion, error) {
	var v PromptPackVersion
	var nodes []byte
	err := row.Scan(&v.ID, &v.VersionID, &v.WorkspaceID, &v.Key, &v.Version, &v.Name, &v.Description, &v.Workflow, &nodes, &v.Status, &v.ContentHash, &v.CreatedAt)
	if err == nil && len(nodes) > 0 {
		v.Nodes = nodes
	}
	return v, err
}

// Principal represents the authenticated user context
type Principal struct {
	WorkspaceID string
	UserID      string
}

// Repository provides prompt catalog CRUD operations
type Repository struct {
	pool interface {
		Begin(context.Context) (pgx.Tx, error)
	}
	writeAudit       func(context.Context, pgx.Tx, Principal, string, string, string) error
	validatePrompt   func(context.Context, pgx.Tx, PromptVersion) error
	workflowNodes    func(string) ([]string, []string)
	regexpKey        func(string) bool
}

// NewRepository creates a new prompt repository
func NewRepository(
	pool interface {
		Begin(context.Context) (pgx.Tx, error)
	},
	writeAudit func(context.Context, pgx.Tx, Principal, string, string, string) error,
	validatePrompt func(context.Context, pgx.Tx, PromptVersion) error,
	workflowNodes func(string) ([]string, []string),
	regexpKey func(string) bool,
) *Repository {
	return &Repository{
		pool:           pool,
		writeAudit:     writeAudit,
		validatePrompt: validatePrompt,
		workflowNodes:  workflowNodes,
		regexpKey:      regexpKey,
	}
}

type promptWrite struct {
	Key             string           `json:"key"`
	NodeKey         string           `json:"node_key"`
	Name            string           `json:"name"`
	Description     *string          `json:"description"`
	Messages        *[]PromptMessage `json:"messages"`
	VariablesSchema json.RawMessage  `json:"variables_schema"`
	OutputSchema    json.RawMessage  `json:"output_schema"`
	RendererVersion string           `json:"renderer_version"`
	ExpectedVersion int              `json:"expected_version"`
}

type packWrite struct {
	Key             string                    `json:"key"`
	Name            string                    `json:"name"`
	Description     *string                   `json:"description"`
	Workflow        string                    `json:"workflow"`
	Nodes           map[string]json.RawMessage `json:"nodes"`
	ExpectedVersion int                       `json:"expected_version"`
}

// ExecutePromptCatalogOp handles CRUD operations for prompts via catalog protocol
func (repo *Repository) ExecutePromptCatalogOp(
	ctx context.Context,
	p Principal,
	resource string, // "prompts" or "packs"
	parts []string,
	action governance.Action,
	filters map[string]string,
	body json.RawMessage,
) (statusCode int, result any, err error) {
	tx, err := repo.pool.Begin(ctx)
	if err != nil {
		return 0, nil, err
	}
	defer tx.Rollback(ctx)

	_, err = tx.Exec(ctx, "SET LOCAL rls.workspace_id = $1", p.WorkspaceID)
	if err != nil {
		return 0, nil, err
	}

	if resource == "prompts" {
		return repo.executePrompt(ctx, tx, p, parts, action, filters, body)
	} else if resource == "packs" {
		return repo.executePack(ctx, tx, p, parts, action, filters, body)
	}

	return 404, nil, governance.ErrNotFound
}

func (repo *Repository) executePrompt(
	ctx context.Context,
	tx pgx.Tx,
	p Principal,
	parts []string,
	action governance.Action,
	filters map[string]string,
	body json.RawMessage,
) (statusCode int, result any, err error) {
	// List prompts
	if len(parts) == 0 && action == governance.Read {
		rows, err := tx.Query(ctx, promptSelect+`WHERE (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version ORDER BY p.key,p.workspace_id NULLS LAST`, p.WorkspaceID)
		if err != nil {
			return 0, nil, err
		}
		defer rows.Close()
		items := []PromptVersion{}
		for rows.Next() {
			v, e := ScanPrompt(rows)
			if e != nil {
				return 0, nil, e
			}
			if status := filters["status"]; status == "" || status == v.Status {
				items = append(items, v)
			}
		}
		if err = rows.Err(); err != nil {
			return 0, nil, err
		}
		return 200, map[string]any{"items": items, "has_more": false}, tx.Commit(ctx)
	}

	// Get specific prompt
	if len(parts) >= 1 && action == governance.Read {
		id := parts[0]
		if _, e := uuid.Parse(id); e != nil {
			return 404, nil, governance.ErrNotFound
		}

		if len(parts) == 2 && parts[1] == "versions" {
			rows, e := tx.Query(ctx, promptSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) ORDER BY v.version DESC`, p.WorkspaceID, id)
			if e != nil {
				return 0, nil, e
			}
			defer rows.Close()
			items := []PromptVersion{}
			for rows.Next() {
				v, e := ScanPrompt(rows)
				if e != nil {
					return 0, nil, e
				}
				items = append(items, v)
			}
			return 200, map[string]any{"items": items}, tx.Commit(ctx)
		}

		current, err := ScanPrompt(tx.QueryRow(ctx, promptSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version`, p.WorkspaceID, id))
		if err != nil {
			return 0, nil, err
		}
		return 200, current, tx.Commit(ctx)
	}

	// Create or update prompt
	var input promptWrite
	if len(body) > 0 {
		if err := json.Unmarshal(body, &input); err != nil {
			return 422, nil, err
		}
	}

	// Create new prompt
	if len(parts) == 0 && action == governance.Create {
		if !repo.regexpKey(input.Key) {
			return 422, nil, governance.ErrInvalidKey
		}
		if strings.TrimSpace(input.Name) == "" {
			return 422, nil, governance.ErrNameRequired
		}

		id := uuid.NewString()
		versionID := uuid.NewString()

		v := PromptVersion{
			ID:              id,
			VersionID:       versionID,
			WorkspaceID:     p.WorkspaceID,
			Key:             input.Key,
			NodeKey:         input.NodeKey,
			Version:         1,
			Name:            input.Name,
			Status:          "draft",
			RendererVersion: input.RendererVersion,
			CreatedAt:       time.Now().UTC(),
		}

		if input.Description != nil {
			v.Description = *input.Description
		}
		if input.Messages != nil {
			v.Messages = *input.Messages
		}
		if len(input.VariablesSchema) > 0 {
			v.VariablesSchema = input.VariablesSchema
		}
		if len(input.OutputSchema) > 0 {
			v.OutputSchema = input.OutputSchema
		}

		messagesJSON, _ := json.Marshal(v.Messages)
		digest := sha256.Sum256(messagesJSON)
		v.ContentHash = hex.EncodeToString(digest[:])

		_, err = tx.Exec(ctx, "INSERT INTO governance_prompts(id,workspace_id,key,current_version) VALUES($1,$2,$3,1)", id, p.WorkspaceID, input.Key)
		if err == nil {
			_, err = tx.Exec(ctx, "INSERT INTO governance_prompt_versions(id,prompt_id,version,node_key,name,description,messages,variables_schema,output_schema,renderer_version,status,content_hash,created_by,created_at) VALUES($1,$2,1,$3,$4,$5,$6,$7,$8,$9,'draft',$10,$11,$12)",
				versionID, id, v.NodeKey, v.Name, v.Description, messagesJSON, v.VariablesSchema, v.OutputSchema, v.RendererVersion, v.ContentHash, p.UserID, v.CreatedAt)
		}
		if err == nil {
			err = repo.writeAudit(ctx, tx, p, "governance.prompt.created", "prompts", id)
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			return 0, nil, err
		}
		return 201, v, nil
	}

	return 405, nil, governance.ErrMethodNotAllowed
}

func (repo *Repository) executePack(
	ctx context.Context,
	tx pgx.Tx,
	p Principal,
	parts []string,
	action governance.Action,
	filters map[string]string,
	body json.RawMessage,
) (statusCode int, result any, err error) {
	// List packs
	if len(parts) == 0 && action == governance.Read {
		rows, err := tx.Query(ctx, packSelect+`WHERE (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version ORDER BY p.key,p.workspace_id NULLS LAST`, p.WorkspaceID)
		if err != nil {
			return 0, nil, err
		}
		defer rows.Close()
		items := []PromptPackVersion{}
		for rows.Next() {
			v, e := ScanPromptPack(rows)
			if e != nil {
				return 0, nil, e
			}
			if status := filters["status"]; status == "" || status == v.Status {
				items = append(items, v)
			}
		}
		if err = rows.Err(); err != nil {
			return 0, nil, err
		}
		return 200, map[string]any{"items": items, "has_more": false}, tx.Commit(ctx)
	}

	// Get specific pack
	if len(parts) >= 1 && action == governance.Read {
		id := parts[0]
		if _, e := uuid.Parse(id); e != nil {
			return 404, nil, governance.ErrNotFound
		}

		current, err := ScanPromptPack(tx.QueryRow(ctx, packSelect+`WHERE p.id=$2 AND (p.workspace_id=$1 OR p.workspace_id IS NULL) AND v.version=p.current_version`, p.WorkspaceID, id))
		if err != nil {
			return 0, nil, err
		}
		return 200, current, tx.Commit(ctx)
	}

	// Create new pack
	if len(parts) == 0 && action == governance.Create {
		var input packWrite
		if err := json.Unmarshal(body, &input); err != nil {
			return 422, nil, err
		}

		if !repo.regexpKey(input.Key) {
			return 422, nil, governance.ErrInvalidKey
		}
		if strings.TrimSpace(input.Name) == "" {
			return 422, nil, governance.ErrNameRequired
		}

		id := uuid.NewString()
		versionID := uuid.NewString()

		nodesJSON, _ := json.Marshal(input.Nodes)
		digest := sha256.Sum256(nodesJSON)
		contentHash := hex.EncodeToString(digest[:])

		v := PromptPackVersion{
			ID:          id,
			VersionID:   versionID,
			WorkspaceID: p.WorkspaceID,
			Key:         input.Key,
			Version:     1,
			Name:        input.Name,
			Workflow:    input.Workflow,
			Nodes:       nodesJSON,
			Status:      "draft",
			ContentHash: contentHash,
			CreatedAt:   time.Now().UTC(),
		}

		if input.Description != nil {
			v.Description = *input.Description
		}

		_, err = tx.Exec(ctx, "INSERT INTO governance_prompt_packs(id,workspace_id,key,current_version) VALUES($1,$2,$3,1)", id, p.WorkspaceID, input.Key)
		if err == nil {
			_, err = tx.Exec(ctx, "INSERT INTO governance_prompt_pack_versions(id,pack_id,version,name,description,workflow,nodes,status,content_hash,created_by,created_at) VALUES($1,$2,1,$3,$4,$5,$6,'draft',$7,$8,$9)",
				versionID, id, v.Name, v.Description, v.Workflow, nodesJSON, contentHash, p.UserID, v.CreatedAt)
		}
		if err == nil {
			err = repo.writeAudit(ctx, tx, p, "governance.pack.created", "packs", id)
		}
		if err == nil {
			err = tx.Commit(ctx)
		}
		if err != nil {
			return 0, nil, err
		}
		return 201, v, nil
	}

	return 405, nil, governance.ErrMethodNotAllowed
}

// FrozenPrompt retrieves a specific frozen prompt version by ID
func (repo *Repository) FrozenPrompt(ctx context.Context, workspace, id string) (PromptVersion, error) {
	tx, err := repo.pool.Begin(ctx)
	if err != nil {
		return PromptVersion{}, err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, "SET LOCAL rls.workspace_id = $1", workspace)
	if err != nil {
		return PromptVersion{}, err
	}
	return ScanPrompt(tx.QueryRow(ctx, promptSelect+"WHERE v.id::text=$1", id))
}

// FrozenPromptPack retrieves a specific frozen prompt pack version by ID
func (repo *Repository) FrozenPromptPack(ctx context.Context, workspace, id string) (PromptPackVersion, error) {
	tx, err := repo.pool.Begin(ctx)
	if err != nil {
		return PromptPackVersion{}, err
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, "SET LOCAL rls.workspace_id = $1", workspace)
	if err != nil {
		return PromptPackVersion{}, err
	}
	return ScanPromptPack(tx.QueryRow(ctx, packSelect+"WHERE v.id::text=$1", id))
}

// CanonicalPrompt computes canonical form of prompt for content hashing
func CanonicalPrompt(v PromptVersion) ([]byte, error) {
	type canonicalMessage struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	}
	messages := make([]canonicalMessage, len(v.Messages))
	for i, m := range v.Messages {
		messages[i] = canonicalMessage{Role: m.Role, Content: m.Content}
	}
	sort.Slice(messages, func(i, j int) bool {
		if messages[i].Role != messages[j].Role {
			return messages[i].Role < messages[j].Role
		}
		return messages[i].Content < messages[j].Content
	})
	return json.Marshal(map[string]any{
		"node_key":         v.NodeKey,
		"messages":         messages,
		"variables_schema": v.VariablesSchema,
		"output_schema":    v.OutputSchema,
		"renderer_version": v.RendererVersion,
	})
}
