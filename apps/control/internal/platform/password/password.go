package password

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"strings"

	"golang.org/x/crypto/bcrypt"
	"golang.org/x/crypto/scrypt"
)

type Codec struct{}

func (Codec) Hash(password string) (string, error) { return Hash(password) }
func (Codec) Verify(encoded, password string) bool { return Verify(encoded, password) }

func Hash(password string) (string, error) {
	if len(password) > 72 {
		salt := make([]byte, 16)
		if _, err := rand.Read(salt); err != nil {
			return "", err
		}
		digest, err := scrypt.Key([]byte(password), salt, 16384, 8, 1, 64)
		if err != nil {
			return "", err
		}
		return "scrypt$16384$8$1$" + base64.RawURLEncoding.EncodeToString(salt) + "$" + base64.RawURLEncoding.EncodeToString(digest), nil
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), bcrypt.DefaultCost)
	return string(hash), err
}

func Verify(encoded, password string) bool {
	if strings.HasPrefix(encoded, "scrypt$") {
		parts := strings.Split(encoded, "$")
		if len(parts) != 6 || parts[1] != "16384" || parts[2] != "8" || parts[3] != "1" {
			return false
		}
		salt, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[4], "="))
		if err != nil || len(salt) < 8 || len(salt) > 64 {
			return false
		}
		expected, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[5], "="))
		if err != nil || len(expected) != 64 {
			return false
		}
		actual, err := scrypt.Key([]byte(password), salt, 16384, 8, 1, len(expected))
		return err == nil && subtle.ConstantTimeCompare(actual, expected) == 1
	}
	return bcrypt.CompareHashAndPassword([]byte(encoded), []byte(password)) == nil
}
