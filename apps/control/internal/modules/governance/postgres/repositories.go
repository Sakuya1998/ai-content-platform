package postgres

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/governance"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/modules/identity"
)

type ModelTester interface {
	Test(context.Context, governance.ModelProfile, governance.ModelTest) (map[string]any, error)
}
type AuditWriter func(context.Context, pgx.Tx, identity.Principal, string, string, string) error

type GovernanceRepository struct {
	pool             *pgxpool.Pool
	key              []byte
	promptCatalog    *PromptRepository
	creationPolicies *PolicyRepository
	tester           ModelTester
	audit            AuditWriter
}
type PromptRepository struct {
	pool  *pgxpool.Pool
	audit AuditWriter
}
type PolicyRepository struct {
	pool  *pgxpool.Pool
	audit AuditWriter
}

func (g *GovernanceRepository) SetPromptCatalog(prompts *PromptRepository) { g.promptCatalog = prompts }
func (g *GovernanceRepository) SetCreationPolicies(policies *PolicyRepository) {
	g.creationPolicies = policies
}

func NewRepositories(pool *pgxpool.Pool, encryptionKey string, tester ModelTester, audit ...AuditWriter) (*GovernanceRepository, *PromptRepository, *PolicyRepository) {
	key, err := hex.DecodeString(encryptionKey)
	if err != nil || len(key) != 32 {
		key, err = base64.StdEncoding.DecodeString(encryptionKey)
	}
	if err != nil || len(key) != 32 {
		key = nil
	}
	var writer AuditWriter
	if len(audit) > 0 {
		writer = audit[0]
	}
	prompts := &PromptRepository{pool: pool, audit: writer}
	policies := &PolicyRepository{pool: pool, audit: writer}
	return &GovernanceRepository{pool: pool, key: key, promptCatalog: prompts, creationPolicies: policies, tester: tester, audit: writer}, prompts, policies
}

func (g *GovernanceRepository) writeAudit(ctx context.Context, tx pgx.Tx, principal identity.Principal, action, resourceType, resourceID string) error {
	if g.audit == nil {
		return errors.New("audit writer unavailable")
	}
	return g.audit(ctx, tx, principal, action, resourceType, resourceID)
}

func (p *PromptRepository) writeAudit(ctx context.Context, tx pgx.Tx, principal identity.Principal, action, resourceType, resourceID string) error {
	if p.audit == nil {
		return errors.New("audit writer unavailable")
	}
	return p.audit(ctx, tx, principal, action, resourceType, resourceID)
}

func (p *PolicyRepository) writeAudit(ctx context.Context, tx pgx.Tx, principal identity.Principal, action, resourceType, resourceID string) error {
	if p.audit == nil {
		return errors.New("audit writer unavailable")
	}
	return p.audit(ctx, tx, principal, action, resourceType, resourceID)
}
func (g *GovernanceRepository) EncryptSecret(secret string) ([]byte, error) {
	return g.encryptSecret(secret)
}
func (g *GovernanceRepository) DecryptSecret(secret []byte) (string, error) {
	return g.decryptSecret(secret)
}
func (g *GovernanceRepository) encryptSecret(secret string) ([]byte, error) {
	if secret == "" {
		return []byte{}, nil
	}
	if len(g.key) != 32 {
		return nil, errors.New("model encryption key must encode 32 bytes")
	}
	block, err := aes.NewCipher(g.key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err = rand.Read(nonce); err != nil {
		return nil, err
	}
	return aead.Seal(nonce, nonce, []byte(secret), []byte("novel-agent:model-key:v1")), nil
}
func (g *GovernanceRepository) decryptSecret(encrypted []byte) (string, error) {
	if len(encrypted) == 0 {
		return "", nil
	}
	if len(g.key) != 32 {
		return "", errors.New("model encryption key must encode 32 bytes")
	}
	block, err := aes.NewCipher(g.key)
	if err != nil {
		return "", err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return "", err
	}
	if len(encrypted) < aead.NonceSize() {
		return "", errors.New("invalid model ciphertext")
	}
	secret, err := aead.Open(nil, encrypted[:aead.NonceSize()], encrypted[aead.NonceSize():], []byte("novel-agent:model-key:v1"))
	return string(secret), err
}
func (g *GovernanceRepository) ExecuteResource(ctx context.Context, p identity.Principal, kind string, command governance.CatalogCommand) (governance.CatalogResult, error) {
	out := &catalogResult{}
	g.executeResource(out, &catalogRequest{ctx: ctx, action: command.Action, payload: command.Payload, filters: command.Filters}, p, kind, command.Path)
	return out.result, nil
}
func (g *GovernanceRepository) ExecuteModel(ctx context.Context, p identity.Principal, command governance.CatalogCommand) (governance.CatalogResult, error) {
	out := &catalogResult{}
	g.executeModel(out, &catalogRequest{ctx: ctx, action: command.Action, payload: command.Payload, filters: command.Filters}, p, "/"+strings.Join(command.Path, "/"))
	return out.result, nil
}
func (g *PromptRepository) ExecutePrompt(ctx context.Context, p identity.Principal, kind string, command governance.CatalogCommand) (governance.CatalogResult, error) {
	tx, err := workspaceTx(ctx, g.pool, p.WorkspaceID)
	if err != nil {
		return governance.CatalogResult{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	out := &catalogResult{}
	request := &catalogRequest{ctx: ctx, action: command.Action, payload: command.Payload, filters: command.Filters}
	switch kind {
	case "prompts":
		g.executePrompt(out, request, p, tx, command.Path)
	case "prompt-packs", "prompt_packs":
		g.executePack(out, request, p, tx, command.Path)
	default:
		g.executeBinding(out, request, p, tx, command.Path)
	}
	return out.result, nil
}
func (g *PolicyRepository) ExecutePolicy(ctx context.Context, p identity.Principal, kind string, command governance.CatalogCommand) (governance.CatalogResult, error) {
	tx, err := workspaceTx(ctx, g.pool, p.WorkspaceID)
	if err != nil {
		return governance.CatalogResult{}, err
	}
	defer func() { _ = tx.Rollback(ctx) }()
	out := &catalogResult{}
	request := &catalogRequest{ctx: ctx, action: command.Action, payload: command.Payload, filters: command.Filters}
	if kind == "creation-policies" {
		g.executePolicy(out, request, p, tx, command.Path)
	} else {
		g.executeBinding(out, request, p, tx, command.Path)
	}
	return out.result, nil
}
func FrozenModelProfile(ctx context.Context, tx pgx.Tx, workspace, id string) (governance.ModelProfile, error) {
	return scanModelProfile(tx.QueryRow(ctx, modelProfileSelect+"WHERE p.workspace_id=$1 AND v.id::text=$2", workspace, id))
}
func (g *GovernanceRepository) testModel(out *catalogResult, request *catalogRequest, p ModelProfile) {
	var input governance.ModelTest
	if !decodeCatalogJSON(out, request, &input) {
		return
	}
	secret, err := g.decryptSecret(p.EncryptedKey)
	if err != nil {
		catalogReject(out, 503, "model key unavailable")
		return
	}
	p.APIKey = secret
	if g.tester == nil {
		catalogReject(out, 503, "model tester unavailable")
		return
	}
	result, err := g.tester.Test(request.Context(), p, input)
	if err != nil {
		catalogReject(out, 422, err.Error())
		return
	}
	catalogComplete(out, 200, result)
}
