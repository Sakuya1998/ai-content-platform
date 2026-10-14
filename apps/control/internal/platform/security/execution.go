package security

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"io"
	"strings"
)

type ExecutionCipher struct{ executionKey []byte }

var executionCredentialAAD = []byte("agent-platform:execution-credential:v1")

func NewExecutionCipher(value string) (*ExecutionCipher, error) {
	g := &ExecutionCipher{}
	if strings.TrimSpace(value) == "" {
		g.executionKey = nil
		return g, nil
	}
	key, err := decodeExecutionEncryptionKey(value)
	if err != nil {
		return nil, err
	}
	g.executionKey = key
	return g, nil
}

func decodeExecutionEncryptionKey(value string) ([]byte, error) {
	key, err := hex.DecodeString(value)
	if err == nil && len(key) == 32 {
		return key, nil
	}
	key, err = base64.StdEncoding.DecodeString(value)
	if err == nil && len(key) == 32 {
		return key, nil
	}
	return nil, errors.New("execution encryption key must encode 32 bytes as hex or base64")
}

func (g *ExecutionCipher) executionCredentialCipher() (cipher.AEAD, error) {
	if len(g.executionKey) != 32 {
		return nil, errors.New("execution encryption key is not configured")
	}
	block, err := aes.NewCipher(g.executionKey)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

func (g *ExecutionCipher) Encrypt(secret string) (string, error) {
	if secret == "" {
		return "", nil
	}
	aead, err := g.executionCredentialCipher()
	if err != nil {
		return "", err
	}
	nonce := make([]byte, aead.NonceSize())
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return "", err
	}
	sealed := aead.Seal(nonce, nonce, []byte(secret), executionCredentialAAD)
	return base64.StdEncoding.EncodeToString(sealed), nil
}

func (g *ExecutionCipher) Decrypt(envelope string) (string, error) {
	sealed, err := base64.StdEncoding.DecodeString(envelope)
	if err != nil {
		return "", err
	}
	aead, err := g.executionCredentialCipher()
	if err != nil {
		return "", err
	}
	if len(sealed) < aead.NonceSize() {
		return "", errors.New("invalid execution credential envelope")
	}
	nonce := sealed[:aead.NonceSize()]
	plaintext, err := aead.Open(nil, nonce, sealed[aead.NonceSize():], executionCredentialAAD)
	if err != nil {
		return "", err
	}
	return string(plaintext), nil
}
