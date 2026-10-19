package prompts

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"sort"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/database"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

type PromptMessage = governance.PromptMessage
type PromptVersion = governance.PromptVersion
type PromptPackEntry = governance.PromptPackEntry
type PromptPackVersion = governance.PromptPackVersion
type FrozenPromptBinding = governance.FrozenPromptBinding
type Principal = identity.Principal

var promptInvalid = governance.PromptInvalid
var canonicalPromptJSON = governance.CanonicalJSON
var workflowPromptNodes = governance.WorkflowPromptNodes
var promptHash = governance.PromptHash
var shaPromptBytes = governance.ShaPromptBytes
var verifyPromptHash = governance.VerifyPromptHash
var compilePromptSchema = governance.CompilePromptSchema
var validatePromptContract = governance.ValidatePromptContract
var renderPromptPreview = governance.RenderPromptPreview

type Repository struct {
	pool  *pgxpool.Pool
	audit func(context.Context, pgx.Tx, Principal, string, string, string) error
}

func NewRepository(pool *pgxpool.Pool, audit func(context.Context, pgx.Tx, Principal, string, string, string) error) *Repository {
	return &Repository{pool: pool, audit: audit}
}

func (repo *Repository) writeAudit(ctx context.Context, tx pgx.Tx, principal Principal, action, resourceType, resourceID string) error {
	if repo.audit == nil {
		return errors.New("audit writer unavailable")
	}
	return repo.audit(ctx, tx, principal, action, resourceType, resourceID)
}

func (repo *Repository) ExecutePrompt(ctx context.Context, principal Principal, kind string, command governance.CatalogCommand) (governance.CatalogResult, error) {
	tx, err := workspaceTx(ctx, repo.pool, principal.WorkspaceID)
	if err != nil {
		return governance.CatalogResult{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	out := &catalogResult{}
	request := &catalogRequest{ctx: ctx, action: command.Action, payload: command.Payload, filters: command.Filters}
	switch kind {
	case "prompts":
		repo.executePrompt(out, request, principal, tx, command.Path)
	case "prompt-packs", "prompt_packs":
		repo.executePack(out, request, principal, tx, command.Path)
	default:
		repo.executeBinding(out, request, principal, tx, command.Path)
	}
	return out.result, nil
}

type catalogRequest struct {
	ctx     context.Context
	action  governance.Action
	payload json.RawMessage
	filters map[string]string
}

func (request *catalogRequest) Context() context.Context { return request.ctx }

type catalogResult struct{ result governance.CatalogResult }

func catalogComplete(result *catalogResult, code int, value any) {
	kind := map[int]string{200: "ok", 201: "created", 204: "empty", 400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "missing", 405: "unsupported", 409: "conflict", 413: "too_large", 422: "invalid", 503: "unavailable"}[code]
	if kind == "" {
		kind = "unavailable"
	}
	result.result = governance.CatalogResult{Kind: kind, Value: value}
}

func catalogReject(result *catalogResult, code int, message string) {
	catalogComplete(result, code, map[string]string{"error": message})
}

func catalogNotFound(result *catalogResult)    { catalogReject(result, 404, "resource not found") }
func catalogUnsupported(result *catalogResult) { catalogReject(result, 405, "operation not supported") }

func catalogDBError(result *catalogResult, err error) {
	var pgerr *pgconn.PgError
	if errors.Is(err, pgx.ErrNoRows) {
		catalogReject(result, 404, "resource not found")
	} else if errors.As(err, &pgerr) && (pgerr.Code == "23505" || pgerr.Code == "23503") {
		catalogReject(result, 409, "resource conflict")
	} else {
		catalogReject(result, 503, "storage unavailable")
	}
}

func catalogPromptError(result *catalogResult, err error) {
	var invalid *governance.PromptValidationError
	if errors.As(err, &invalid) {
		catalogReject(result, 422, invalid.Error())
		return
	}
	catalogDBError(result, err)
}

func decodeCatalogJSON(result *catalogResult, request *catalogRequest, value any) bool {
	decoder := json.NewDecoder(bytes.NewReader(request.payload))
	decoder.DisallowUnknownFields()
	if decoder.Decode(value) != nil {
		catalogReject(result, 400, "invalid request body")
		return false
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		catalogReject(result, 400, "request body must contain one JSON object")
		return false
	}
	return true
}

func decodeCatalogBody(request *catalogRequest, value any) error {
	decoder := json.NewDecoder(bytes.NewReader(request.payload))
	decoder.DisallowUnknownFields()
	decoder.UseNumber()
	if err := decoder.Decode(value); err != nil {
		return governance.PromptInvalid("invalid request body: %v", err)
	}
	var extra any
	if decoder.Decode(&extra) != io.EOF {
		return governance.PromptInvalid("request body must contain one JSON object")
	}
	return nil
}

func workspaceTx(ctx context.Context, pool *pgxpool.Pool, workspace string) (pgx.Tx, error) {
	if pool == nil {
		return nil, errors.New("catalog storage unavailable")
	}
	return database.BeginWorkspaceTx(ctx, pool, workspace)
}

func CanonicalPrompt(version PromptVersion) ([]byte, error) {
	type canonicalMessage struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	}
	messages := make([]canonicalMessage, len(version.Messages))
	for i, message := range version.Messages {
		messages[i] = canonicalMessage{Role: message.Role, Content: message.Content}
	}
	sort.Slice(messages, func(i, j int) bool {
		if messages[i].Role != messages[j].Role {
			return messages[i].Role < messages[j].Role
		}
		return messages[i].Content < messages[j].Content
	})
	return json.Marshal(map[string]any{
		"node_key":         version.NodeKey,
		"messages":         messages,
		"variables_schema": version.VariablesSchema,
		"output_schema":    version.OutputSchema,
		"renderer_version": version.RendererVersion,
	})
}
