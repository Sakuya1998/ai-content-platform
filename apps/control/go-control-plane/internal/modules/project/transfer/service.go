package transfer

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

type Transfer struct {
	ProjectID, Status, Format, Hash, Failure string
	Body                                     []byte
	Claimed                                  time.Time
}
type Repository interface {
	Backup(context.Context, string, string) (Backup, error)
	QueueTransfer(context.Context, string, string, string, string, []byte) error
	ClaimTransfer(context.Context, string, string) (Transfer, bool, error)
	FinishTransfer(context.Context, string, string, time.Time, []byte, bool) error
	Transfer(context.Context, string, string) (Transfer, error)
	CancelTransfer(context.Context, string, string) error
	PendingTransfers(context.Context) (map[string][]string, error)
}
type Service struct {
	Repository  Repository
	transferKey []byte
	slots       chan struct{}
}

func NewService(repo Repository, key string) *Service {
	service := &Service{Repository: repo, slots: make(chan struct{}, 2)}
	service.configureKey(key)
	return service
}

func (a *Service) configureKey(key string) {
	decoded, err := hex.DecodeString(key)
	if err != nil || len(decoded) != 32 {
		decoded, err = base64.StdEncoding.DecodeString(key)
	}
	if err == nil && len(decoded) == 32 {
		a.transferKey = decoded
	}
}

type resourcePendingTransfer struct {
	Backup   Backup            `json:"backup"`
	Metadata map[string]string `json:"metadata"`
	Secret   []byte            `json:"encrypted_secret,omitempty"`
}

func (a *Service) aead() (cipher.AEAD, error) {
	if len(a.transferKey) != 32 {
		return nil, errors.New("transfer encryption key unavailable")
	}
	block, err := aes.NewCipher(a.transferKey)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}
func (a *Service) EncodePending(backup Backup, metadata map[string]string, password string) ([]byte, error) {
	request := resourcePendingTransfer{Backup: backup, Metadata: metadata}
	if password != "" {
		aead, err := a.aead()
		if err != nil {
			return nil, err
		}
		nonce := make([]byte, aead.NonceSize())
		if _, err = rand.Read(nonce); err != nil {
			return nil, err
		}
		request.Secret = aead.Seal(nonce, nonce, []byte(password), []byte("novel-agent:transfer-password:v1:"+backup.SHA256))
	}
	return resourceJSON(request), nil
}
func (a *Service) DecodePending(body []byte) (Backup, map[string]string, string, error) {
	var request resourcePendingTransfer
	if json.Unmarshal(body, &request) != nil || request.Backup.SHA256 != Hash(request.Backup) {
		return Backup{}, nil, "", errResourceInvalid
	}
	password := ""
	if len(request.Secret) > 0 {
		aead, err := a.aead()
		if err != nil {
			return Backup{}, nil, "", err
		}
		if len(request.Secret) < aead.NonceSize() {
			return Backup{}, nil, "", errResourceInvalid
		}
		secret, err := aead.Open(nil, request.Secret[:aead.NonceSize()], request.Secret[aead.NonceSize():], []byte("novel-agent:transfer-password:v1:"+request.Backup.SHA256))
		if err != nil {
			return Backup{}, nil, "", errResourceInvalid
		}
		password = string(secret)
	}
	return request.Backup, request.Metadata, password, nil
}
func Render(backup Backup, format string, metadata map[string]string) ([]byte, error) {
	if format == "backup" {
		return EncodeZIP(backup)
	}
	if format == "docx" || format == "epub" {
		return RenderPublication(backup, format, metadata)
	}
	if format != "txt" && format != "markdown" {
		return nil, errResourceInvalid
	}
	var output strings.Builder
	if format == "markdown" {
		output.WriteString("# ")
	}
	output.WriteString(resourceText(backup.ProjectResource["title"]) + "\n\n")
	for _, chapter := range Blocks(backup) {
		if format == "markdown" {
			output.WriteString("## ")
		}
		output.WriteString(fmt.Sprintf("%d. %s\n\n%s\n\n", resourceInt(chapter["block_index"]), resourceText(chapter["title"]), resourceText(chapter["content"])))
	}
	return []byte(output.String()), nil
}

// Recover resumes durable exports from their captured snapshot. Stale
// claims are fenced by their timestamp, so an old worker cannot overwrite a retry.
func (a *Service) Recover(ctx context.Context) error {
	pending, err := a.Repository.PendingTransfers(ctx)
	if err != nil {
		return err
	}
	for scope, ids := range pending {
		for _, id := range ids {
			go a.Execute(scope, id)
		}
	}
	return nil
}
func (a *Service) Execute(scope, id string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	select {
	case a.slots <- struct{}{}:
		defer func() { <-a.slots }()
	case <-ctx.Done():
		return
	}
	claim, started, err := a.Repository.ClaimTransfer(ctx, scope, id)
	pending, format, claimed := claim.Body, claim.Format, claim.Claimed
	if err != nil || !started {
		return
	}
	backup, metadata, password, exportErr := a.DecodePending(pending)
	var body []byte
	if exportErr == nil {
		body, exportErr = Render(backup, format, metadata)
	}
	if exportErr == nil && password != "" {
		body, exportErr = Encrypt(body, password)
	}
	if exportErr == nil && ctx.Err() != nil {
		exportErr = ctx.Err()
	}
	finishCtx, finishCancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer finishCancel()
	_ = a.Repository.FinishTransfer(finishCtx, scope, id, claimed, body, exportErr != nil)
}
