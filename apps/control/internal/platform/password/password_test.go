package password

import (
	"encoding/base64"
	"strings"
	"testing"

	"golang.org/x/crypto/scrypt"
)

func TestPasswordHashVerification(t *testing.T) {
	hash, err := Hash("correct horse battery staple")
	if err != nil {
		t.Fatal(err)
	}
	if !Verify(hash, "correct horse battery staple") || Verify(hash, "wrong") {
		t.Fatal("password hash verification failed")
	}
}

func TestImportedScryptPasswordAndBoundedParameters(t *testing.T) {
	salt := []byte("test-only-salt-16")
	digest, err := scrypt.Key([]byte("legacy-password"), salt, 16384, 8, 1, 64)
	if err != nil {
		t.Fatal(err)
	}
	encoded := "scrypt$16384$8$1$" + base64.RawURLEncoding.EncodeToString(salt) + "$" + base64.RawURLEncoding.EncodeToString(digest)
	if !Verify(encoded, "legacy-password") || Verify(encoded, "wrong") {
		t.Fatal("imported password verification failed")
	}
	if Verify("scrypt$1073741824$8$1$AAAA$AAAA", "anything") {
		t.Fatal("unsafe KDF parameters accepted")
	}
}

func TestLongPasswordIsNotTruncated(t *testing.T) {
	value := strings.Repeat("a", 120)
	hash, err := Hash(value)
	if err != nil || !Verify(hash, value) || Verify(hash, value+"b") {
		t.Fatal("long password was truncated or rejected")
	}
}
