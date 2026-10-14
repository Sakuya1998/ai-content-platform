package objectstorage

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/config"
)

func TestS3RequestsDoNotSignProxyMutableEncodingHeader(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.Contains(r.Header.Get("Authorization"), "accept-encoding") {
			w.WriteHeader(http.StatusForbidden)
			return
		}
		if r.Header.Get("Accept-Encoding") != "" {
			t.Error("object requests must not negotiate compression")
		}
		w.Header().Set("Content-Type", "text/plain")
		if r.Method == http.MethodGet {
			_, _ = w.Write([]byte("artifact"))
		}
	}))
	defer server.Close()
	store, err := NewS3Store(config.ObjectStorageProvider{Endpoint: server.URL, Region: "us-east-1", Bucket: "agent-artifacts", AccessKey: "test", SecretKey: "test-secret"})
	if err != nil {
		t.Fatal(err)
	}
	if err = store.Ready(context.Background()); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("artifact"))
	if _, err = store.Get(context.Background(), "artifact.txt", hex.EncodeToString(sum[:])); err != nil {
		t.Fatal(err)
	}
	if _, err = store.Put(context.Background(), "artifact.txt", []byte("artifact"), "text/plain"); err != nil {
		t.Fatal(err)
	}
}

func TestS3WritesSharedBucketWithImmutableChecksum(t *testing.T) {
	data := []byte("generated text")
	sum := sha256.Sum256(data)
	hash := hex.EncodeToString(sum[:])
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut {
			t.Errorf("unexpected request: %s", r.Method)
		}
		if r.URL.Path != "/agent-artifacts/workspaces/ws-1/projects/p-1/assets/a-1/versions/v-1/content.txt" {
			t.Errorf("wrong object path: %s", r.URL.Path)
		}
		body, _ := io.ReadAll(r.Body)
		if r.Header.Get("X-Amz-Checksum-Sha256") != base64.StdEncoding.EncodeToString(sum[:]) {
			t.Error("upload must send the SHA256 checksum for server-side validation")
		}
		if !bytes.Equal(body, data) || r.Header.Get("If-None-Match") != "*" || r.Header.Get("X-Amz-Meta-Sha256") != hash {
			t.Errorf("immutable upload: body=%q conditional=%q hash=%q", body, r.Header.Get("If-None-Match"), r.Header.Get("X-Amz-Meta-Sha256"))
		}
		w.Header().Set("ETag", `"etag"`)
	}))
	defer server.Close()
	store, err := NewS3Store(config.ObjectStorageProvider{Endpoint: server.URL, Region: "us-east-1", Bucket: "agent-artifacts", AccessKey: "test", SecretKey: "test-secret"})
	if err != nil {
		t.Fatal(err)
	}
	key, _ := ObjectKey("ws-1", "p-1", "assets", "a-1", "content.txt", "v-1")
	ref, err := store.Put(context.Background(), key, data, "text/plain")
	if err != nil || ref.SHA256 != hash || ref.Bucket != "agent-artifacts" {
		t.Fatalf("upload: %#v %v", ref, err)
	}
}

func TestS3ConditionalUploadReplay(t *testing.T) {
	data := []byte("generated text")
	sum := sha256.Sum256(data)
	digest := hex.EncodeToString(sum[:])
	for _, tc := range []struct {
		name, checksum, size, contentType string
		conflict                          bool
	}{
		{"identical", digest, "14", "text/plain", false},
		{"different checksum", strings.Repeat("b", 64), "14", "text/plain", true},
		{"different size", digest, "15", "text/plain", true},
		{"different content type", digest, "14", "application/json", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.Method {
				case http.MethodPut:
					w.WriteHeader(http.StatusPreconditionFailed)
				case http.MethodHead:
					w.Header().Set("X-Amz-Meta-Sha256", tc.checksum)
					w.Header().Set("Content-Length", tc.size)
					w.Header().Set("Content-Type", tc.contentType)
					w.Header().Set("ETag", `"existing-etag"`)
				default:
					t.Errorf("unexpected method: %s", r.Method)
				}
			}))
			defer server.Close()
			store, err := NewS3Store(config.ObjectStorageProvider{Endpoint: server.URL, AccessKey: "test", SecretKey: "test-secret"})
			if err != nil {
				t.Fatal(err)
			}
			ref, err := store.Put(context.Background(), "artifact.txt", data, "text/plain")
			if tc.conflict {
				if !errors.Is(err, ErrArtifactConflict) {
					t.Fatalf("expected conflict, got %v", err)
				}
			} else if err != nil || ref.SHA256 != digest || ref.ETag != "existing-etag" {
				t.Fatalf("idempotent replay: %#v %v", ref, err)
			}
		})
	}
}

func TestS3DownloadVerifiesChecksum(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("unexpected method: %s", r.Method)
		}
		_, _ = w.Write([]byte("hello"))
	}))
	defer server.Close()
	store, err := NewS3Store(config.ObjectStorageProvider{Endpoint: server.URL, AccessKey: "test", SecretKey: "test-secret"})
	if err != nil {
		t.Fatal(err)
	}
	data, err := store.Get(context.Background(), "artifact.txt", "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
	if err != nil || string(data) != "hello" {
		t.Fatalf("download: %q %v", data, err)
	}
	if _, err = store.Get(context.Background(), "artifact.txt", strings.Repeat("a", 64)); !errors.Is(err, ErrArtifactConflict) {
		t.Fatalf("expected checksum conflict, got %v", err)
	}
}

func TestS3DownloadRejectsOversizedOrConflictingMetadata(t *testing.T) {
	for _, oversized := range []bool{true, false} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if oversized {
				w.Header().Set("Content-Length", strconv.FormatInt(maxDownloadBytes+1, 10))
			} else {
				w.Header().Set("X-Amz-Meta-Sha256", strings.Repeat("a", 64))
			}
			_, _ = w.Write([]byte("hello"))
		}))
		store, err := NewS3Store(config.ObjectStorageProvider{Endpoint: server.URL, AccessKey: "test", SecretKey: "test-secret"})
		if err != nil {
			t.Fatal(err)
		}
		_, err = store.Get(context.Background(), "artifact.txt", "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
		server.Close()
		if !errors.Is(err, ErrArtifactConflict) {
			t.Fatalf("conflicting object was downloaded: %v", err)
		}
	}
}

// Run explicitly against a disposable RustFS instance with an initialized bucket.
func TestS3RustFSIntegration(t *testing.T) {
	endpoint := os.Getenv("ARTIFACT_TEST_S3_ENDPOINT")
	if endpoint == "" {
		t.Skip("ARTIFACT_TEST_S3_ENDPOINT is not configured")
	}
	store, err := NewS3Store(config.ObjectStorageProvider{
		Endpoint: endpoint, AccessKey: os.Getenv("ARTIFACT_TEST_S3_ACCESS_KEY"),
		SecretKey: os.Getenv("ARTIFACT_TEST_S3_SECRET_KEY"),
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if err = store.Ready(ctx); err != nil {
		t.Fatal(err)
	}
	key := "workspaces/ws-check/projects/go-sdk/assets/direct/versions/" + time.Now().Format("20060102150405.000000000") + "/content.txt"
	data := []byte("AWS SDK Go v2 RustFS integration")
	ref, err := store.Put(ctx, key, data, "text/plain")
	if err != nil {
		t.Fatal(err)
	}
	replay, err := store.Put(ctx, key, data, "text/plain")
	if err != nil || replay != ref {
		t.Fatalf("replay: %#v %v", replay, err)
	}
	if _, err = store.Put(ctx, key, []byte("changed"), "text/plain"); !errors.Is(err, ErrArtifactConflict) {
		t.Fatalf("expected immutable conflict, got %v", err)
	}
	actual, err := store.Get(ctx, key, ref.SHA256)
	if err != nil || !bytes.Equal(actual, data) {
		t.Fatalf("download: %q %v", actual, err)
	}
}

func TestPresignedUploadBindsChecksumAndConditionalHeader(t *testing.T) {
	store, err := NewS3Store(config.ObjectStorageProvider{Endpoint: "http://localhost:9000", Region: "us-east-1", Bucket: "agent-artifacts", AccessKey: "test", SecretKey: "test-secret"})
	if err != nil {
		t.Fatal(err)
	}
	key, _ := ObjectKey("ws-1", "p-1", "uploads", "a-1", "file.txt", "")
	signed, err := store.PresignUpload(context.Background(), key, strings.Repeat("a", 64), "text/plain", 5*time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if signed.Headers["If-None-Match"] != "*" || !strings.Contains(signed.URL, "X-Amz-Signature=") {
		t.Fatal("upload is not conditionally signed")
	}
	parsed, _ := url.Parse(signed.URL)
	for _, name := range []string{"content-type", "if-none-match", "x-amz-meta-sha256", "x-amz-checksum-sha256"} {
		if !strings.Contains(parsed.Query().Get("X-Amz-SignedHeaders"), name) {
			t.Errorf("upload signature does not bind %s", name)
		}
	}
	decoded, _ := hex.DecodeString(strings.Repeat("a", 64))
	if signed.Headers["Content-Type"] != "text/plain" || signed.Headers["X-Amz-Checksum-Sha256"] != base64.StdEncoding.EncodeToString(decoded) {
		t.Fatal("required upload headers missing")
	}
}

func TestPublicEndpointControlsSignedURL(t *testing.T) {
	store, err := NewS3Store(config.ObjectStorageProvider{Endpoint: "http://rustfs:9000", PublicEndpoint: "https://artifacts.example.com", Region: "us-east-1", Bucket: "agent-artifacts", AccessKey: "test", SecretKey: "test-secret"})
	if err != nil {
		t.Fatal(err)
	}
	signed, err := store.PresignDownload(context.Background(), "workspaces/ws-1/projects/p-1/uploads/a-1/file.txt", time.Minute)
	if err != nil || !strings.HasPrefix(signed.URL, "https://artifacts.example.com/agent-artifacts/") {
		t.Fatal("signed URL did not use the public endpoint")
	}
}
