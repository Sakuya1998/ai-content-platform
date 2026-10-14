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
	"net/url"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/aws/signer/v4"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/smithy-go/middleware"
	smithyhttp "github.com/aws/smithy-go/transport/http"
	"github.com/Sakuya1998/ai-content-platform/apps/control/internal/platform/config"
)

var ErrArtifactConflict = errors.New("artifact version checksum conflict")

const maxDownloadBytes int64 = 50 << 20

type ArtifactReference struct {
	Bucket      string `json:"bucket"`
	ObjectKey   string `json:"object_key"`
	SHA256      string `json:"sha256"`
	SizeBytes   int64  `json:"size_bytes"`
	ContentType string `json:"content_type"`
	ETag        string `json:"etag"`
}

type SignedURL struct {
	URL              string            `json:"url"`
	Headers          map[string]string `json:"headers"`
	Bucket           string            `json:"bucket"`
	ObjectKey        string            `json:"object_key"`
	ExpiresInSeconds int64             `json:"expires_in_seconds"`
}

type S3Store struct {
	client *s3.Client
	signer *s3.PresignClient
	bucket string
}

func NewS3Store(cfg config.ObjectStorageProvider) (*S3Store, error) {
	endpoint, err := url.Parse(cfg.Endpoint)
	if err != nil || endpoint.Host == "" || (endpoint.Scheme != "http" && endpoint.Scheme != "https") {
		return nil, errors.New("S3 endpoint must be an HTTP or HTTPS URL")
	}
	if endpoint.Path != "" && endpoint.Path != "/" {
		return nil, errors.New("S3 endpoint cannot have a path prefix")
	}
	region := cfg.Region
	if region == "" {
		region = "us-east-1"
	}
	bucket := cfg.Bucket
	if bucket == "" {
		bucket = "agent-artifacts"
	}
	newClient := func(origin string) *s3.Client {
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.DisableCompression = true
		return s3.NewFromConfig(aws.Config{
			Region:           region,
			Credentials:      credentials.NewStaticCredentialsProvider(cfg.AccessKey, cfg.SecretKey, ""),
			HTTPClient:       &http.Client{Timeout: 30 * time.Second, Transport: transport},
			RetryMaxAttempts: 3,
		}, func(options *s3.Options) {
			options.BaseEndpoint = aws.String(origin)
			options.UsePathStyle = true
			options.APIOptions = append(options.APIOptions, omitProxyMutableEncoding)
		})
	}
	client := newClient(endpoint.Scheme + "://" + endpoint.Host)
	signingClient := client
	if cfg.PublicEndpoint != "" {
		public, parseErr := url.Parse(cfg.PublicEndpoint)
		if parseErr != nil || public.Host == "" || (public.Scheme != "http" && public.Scheme != "https") ||
			(public.Path != "" && public.Path != "/") {
			return nil, errors.New("public S3 endpoint must be an HTTP or HTTPS origin")
		}
		signingClient = newClient(public.Scheme + "://" + public.Host)
	}
	signer := s3.NewPresignClient(signingClient)
	return &S3Store{client: client, signer: signer, bucket: bucket}, nil
}

func omitProxyMutableEncoding(stack *middleware.Stack) error {
	// CDNs may rewrite Accept-Encoding, so omit it before signing and keep
	// transport compression disabled to preserve object bytes and checksums.
	if _, exists := stack.Finalize.Get("DisableAcceptEncodingGzip"); exists {
		_, err := stack.Finalize.Remove("DisableAcceptEncodingGzip")
		return err
	}
	return nil
}

func (s *S3Store) Ready(ctx context.Context) error {
	_, err := s.client.HeadBucket(ctx, &s3.HeadBucketInput{Bucket: aws.String(s.bucket)})
	return err
}

func (s *S3Store) Put(ctx context.Context, key string, data []byte, contentType string) (ArtifactReference, error) {
	sum := sha256.Sum256(data)
	digest := hex.EncodeToString(sum[:])
	info, err := s.client.PutObject(ctx, &s3.PutObjectInput{
		Bucket: aws.String(s.bucket), Key: aws.String(key), Body: bytes.NewReader(data),
		ContentType: aws.String(contentType), Metadata: map[string]string{"sha256": digest},
		IfNoneMatch: aws.String("*"), ChecksumSHA256: aws.String(base64.StdEncoding.EncodeToString(sum[:])),
	})
	if err != nil {
		var responseError *smithyhttp.ResponseError
		if !errors.As(err, &responseError) || responseError.HTTPStatusCode() != http.StatusPreconditionFailed {
			return ArtifactReference{}, err
		}
		previous, statErr := s.Stat(ctx, key)
		if statErr != nil {
			return ArtifactReference{}, statErr
		}
		if previous.SHA256 != digest || previous.SizeBytes != int64(len(data)) || previous.ContentType != contentType {
			return ArtifactReference{}, ErrArtifactConflict
		}
		return previous, nil
	}
	return ArtifactReference{Bucket: s.bucket, ObjectKey: key, SHA256: digest, SizeBytes: int64(len(data)), ContentType: contentType, ETag: strings.Trim(aws.ToString(info.ETag), "\"")}, nil
}

func (s *S3Store) Stat(ctx context.Context, key string) (ArtifactReference, error) {
	info, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: aws.String(s.bucket), Key: aws.String(key)})
	if err != nil {
		return ArtifactReference{}, err
	}
	return ArtifactReference{Bucket: s.bucket, ObjectKey: key, SHA256: info.Metadata["sha256"], SizeBytes: aws.ToInt64(info.ContentLength), ContentType: aws.ToString(info.ContentType), ETag: strings.Trim(aws.ToString(info.ETag), "\"")}, nil
}

func (s *S3Store) Get(ctx context.Context, key, expectedSHA256 string) ([]byte, error) {
	object, err := s.client.GetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(s.bucket), Key: aws.String(key)})
	if err != nil {
		return nil, err
	}
	defer object.Body.Close()
	if aws.ToInt64(object.ContentLength) > maxDownloadBytes {
		return nil, ErrArtifactConflict
	}
	if metadataHash := object.Metadata["sha256"]; metadataHash != "" && metadataHash != expectedSHA256 {
		return nil, ErrArtifactConflict
	}
	data, err := io.ReadAll(io.LimitReader(object.Body, maxDownloadBytes+1))
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(data)
	if int64(len(data)) > maxDownloadBytes || hex.EncodeToString(sum[:]) != expectedSHA256 {
		return nil, ErrArtifactConflict
	}
	return data, nil
}

func (s *S3Store) PresignUpload(ctx context.Context, key, digest, contentType string, expiry time.Duration) (SignedURL, error) {
	decoded, err := hex.DecodeString(digest)
	if err != nil || len(decoded) != sha256.Size {
		return SignedURL{}, errors.New("SHA256 must have 64 hexadecimal characters")
	}
	signed, err := s.signer.PresignPutObject(ctx, &s3.PutObjectInput{
		Bucket: aws.String(s.bucket), Key: aws.String(key), ContentType: aws.String(contentType),
		IfNoneMatch: aws.String("*"), Metadata: map[string]string{"sha256": strings.ToLower(digest)},
		ChecksumSHA256: aws.String(base64.StdEncoding.EncodeToString(decoded)),
	}, func(options *s3.PresignOptions) {
		options.Expires = expiry
		options.Presigner = uploadPresigner{Signer: v4.NewSigner(), contentType: contentType}
	})
	if err != nil {
		return SignedURL{}, err
	}
	values := map[string]string{}
	for name := range signed.SignedHeader {
		if !strings.EqualFold(name, "Host") {
			values[http.CanonicalHeaderKey(name)] = signed.SignedHeader.Get(name)
		}
	}
	return SignedURL{URL: signed.URL, Headers: values, Bucket: s.bucket, ObjectKey: key, ExpiresInSeconds: int64(expiry.Seconds())}, nil
}

type uploadPresigner struct {
	*v4.Signer
	contentType string
}

func (p uploadPresigner) PresignHTTP(ctx context.Context, credentials aws.Credentials, request *http.Request, payloadHash, service, region string, signingTime time.Time, options ...func(*v4.SignerOptions)) (string, http.Header, error) {
	// S3 presigning removes Content-Type when no body is supplied; bind it explicitly.
	request.Header.Set("Content-Type", p.contentType)
	options = append(options, func(o *v4.SignerOptions) { o.DisableHeaderHoisting = true })
	return p.Signer.PresignHTTP(ctx, credentials, request, payloadHash, service, region, signingTime, options...)
}

func (s *S3Store) PresignDownload(ctx context.Context, key string, expiry time.Duration) (SignedURL, error) {
	signed, err := s.signer.PresignGetObject(ctx, &s3.GetObjectInput{Bucket: aws.String(s.bucket), Key: aws.String(key)}, func(options *s3.PresignOptions) { options.Expires = expiry })
	if err != nil {
		return SignedURL{}, err
	}
	return SignedURL{URL: signed.URL, Headers: map[string]string{}, Bucket: s.bucket, ObjectKey: key, ExpiresInSeconds: int64(expiry.Seconds())}, nil
}
