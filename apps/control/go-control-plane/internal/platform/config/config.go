package config

import (
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"math"
	"net"
	"net/url"
	"strings"

	"github.com/spf13/viper"
)

// Config contains management-plane-only configuration. Secrets should usually
// arrive through CONTROL_PLANE_* environment variables or a mounted secret.
type Config struct {
	Server        ServerConfig        `mapstructure:"server"`
	Database      DatabaseConfig      `mapstructure:"database"`
	Redis         RedisConfig         `mapstructure:"redis"`
	MQ            MQConfig            `mapstructure:"mq"`
	ObjectStorage ObjectStorageConfig `mapstructure:"object_storage"`
	Security      SecurityConfig      `mapstructure:"security"`
}

type SecurityConfig struct {
	InternalToken          string   `mapstructure:"internal_token"`
	AllowMemory            bool     `mapstructure:"allow_memory"`
	AuthEnabled            bool     `mapstructure:"auth_enabled"`
	CookieSecure           bool     `mapstructure:"cookie_secure"`
	SessionHours           int      `mapstructure:"session_hours"`
	AllowedOrigins         []string `mapstructure:"allowed_origins"`
	ModelEncryptionKey     string   `mapstructure:"model_encryption_key"`
	ExecutionEncryptionKey string   `mapstructure:"execution_encryption_key"`
}

type ServerConfig struct {
	Environment string `mapstructure:"environment"`
	Name        string `mapstructure:"name"`
	HTTPAddr    string `mapstructure:"http_addr"`
}

type DatabaseConfig struct {
	URL             string `mapstructure:"url"`
	Addr            string `mapstructure:"addr"`
	DB              string `mapstructure:"db"`
	Username        string `mapstructure:"username"`
	Password        string `mapstructure:"password"`
	MaxConnections  int    `mapstructure:"max_connections"`
	MinConnections  int    `mapstructure:"min_connections"`
	IdleTimeoutSecs int    `mapstructure:"idle_timeout_seconds"`
}

func (d DatabaseConfig) ConnectionURL() string {
	if d.URL != "" {
		return d.URL
	}
	if d.Addr == "" || d.DB == "" || d.Username == "" {
		return ""
	}
	return (&url.URL{Scheme: "postgresql", Host: d.Addr, User: url.UserPassword(d.Username, d.Password), Path: "/" + d.DB}).String()
}

type RedisConfig struct {
	KeyPrefix       string `mapstructure:"key_prefix"`
	Addr            string `mapstructure:"addr"`
	DB              int    `mapstructure:"db"`
	Username        string `mapstructure:"username"`
	Password        string `mapstructure:"password"`
	MaxConnections  int    `mapstructure:"max_connections"`
	MinConnections  int    `mapstructure:"min_connections"`
	IdleTimeoutSecs int    `mapstructure:"idle_timeout_seconds"`
}

type MQConfig struct {
	Enabled    bool     `mapstructure:"enabled"`
	Type       string   `mapstructure:"type"`
	Endpoint   string   `mapstructure:"endpoint"`
	AccessKey  string   `mapstructure:"access_key"`
	SecretKey  string   `mapstructure:"secret_key"`
	Namespace  string   `mapstructure:"namespace"`
	TLSEnabled bool     `mapstructure:"tls_enabled"`
	Topics     MQTopics `mapstructure:"topics"`
	Groups     MQGroups `mapstructure:"groups"`
}
type MQTopics struct {
	Tasks   string `mapstructure:"tasks"`
	Control string `mapstructure:"control"`
	Events  string `mapstructure:"events"`
	Memory  string `mapstructure:"memory"`
}
type MQGroups struct {
	Projection string `mapstructure:"projection"`
}
type ObjectStorageConfig struct {
	Default   string                  `mapstructure:"default"`
	Providers []ObjectStorageProvider `mapstructure:"providers"`
}
type ObjectStorageProvider struct {
	Type           string `mapstructure:"type"`
	Endpoint       string `mapstructure:"endpoint"`
	PublicEndpoint string `mapstructure:"public_endpoint"`
	Region         string `mapstructure:"region"`
	Bucket         string `mapstructure:"bucket"`
	AccessKey      string `mapstructure:"access_key"`
	SecretKey      string `mapstructure:"secret_key"`
}

func Load() (Config, error) {
	v := viper.New()
	v.SetEnvPrefix("CONTROL_PLANE")
	v.SetEnvKeyReplacer(strings.NewReplacer(".", "_", "-", "_"))
	v.SetDefault("server.environment", "development")
	v.SetDefault("security.auth_enabled", true)
	v.SetDefault("security.session_hours", 720)
	v.SetDefault("security.allowed_origins", []string{"http://localhost:5173", "http://127.0.0.1:5173"})
	v.SetDefault("server.name", "control")
	v.SetDefault("server.http_addr", ":8080")
	v.SetDefault("database.max_connections", 100)
	v.SetDefault("database.min_connections", 50)
	v.SetDefault("database.idle_timeout_seconds", 300)
	v.SetDefault("redis.db", 0)
	v.SetDefault("redis.key_prefix", "control:")
	v.SetDefault("redis.max_connections", 100)
	v.SetDefault("redis.min_connections", 50)
	v.SetDefault("redis.idle_timeout_seconds", 300)
	v.SetDefault("mq.type", "rocketmq")
	v.SetDefault("mq.tls_enabled", false)
	v.SetDefault("object_storage.default", "s3")
	for _, key := range []string{
		"config", "server.environment", "server.name", "server.http_addr",
		"database.addr", "database.db", "database.username", "database.password",
		"database.max_connections", "database.min_connections", "database.idle_timeout_seconds",
		"redis.addr", "redis.db", "redis.username", "redis.password", "redis.max_connections",
		"redis.min_connections", "redis.idle_timeout_seconds", "mq.type", "object_storage.default",
		"security.internal_token", "security.allow_memory",
		"security.auth_enabled", "security.cookie_secure", "security.session_hours", "security.allowed_origins", "security.model_encryption_key", "security.execution_encryption_key",
		"redis.key_prefix", "mq.endpoint", "mq.access_key", "mq.secret_key", "mq.namespace",
		"mq.tls_enabled", "mq.enabled", "mq.topics.tasks", "mq.topics.control", "mq.topics.events", "mq.topics.memory", "mq.groups.projection",
		"object_storage.providers.0.type", "object_storage.providers.0.endpoint", "object_storage.providers.0.public_endpoint", "object_storage.providers.0.region", "object_storage.providers.0.bucket", "object_storage.providers.0.access_key", "object_storage.providers.0.secret_key",
	} {
		if err := v.BindEnv(key); err != nil {
			return Config{}, fmt.Errorf("bind control-plane environment %s: %w", key, err)
		}
	}
	// The migration and service contract intentionally share one URL.
	_ = v.BindEnv("database.url", "CONTROL_DATABASE_URL")
	if path := v.GetString("config"); path != "" {
		v.SetConfigFile(path)
	} else {
		v.SetConfigName("config")
		v.SetConfigType("yaml")
		v.AddConfigPath(".")
	}
	if err := v.ReadInConfig(); err != nil {
		if _, ok := err.(viper.ConfigFileNotFoundError); !ok {
			return Config{}, fmt.Errorf("read control-plane config: %w", err)
		}
	}
	var cfg Config
	environment := v.GetString("server.environment")
	if !v.IsSet("security.cookie_secure") && (environment == "production" || environment == "prod") {
		v.SetDefault("security.cookie_secure", true)
	}
	if environment == "development" {
		environment = "dev"
	}
	if environment == "production" {
		environment = "prod"
	}
	for key, suffix := range map[string]string{
		"mq.topics.tasks": "runtime-tasks", "mq.topics.control": "runtime-control",
		"mq.topics.events": "runtime-events", "mq.topics.memory": "memory-tasks",
		"mq.groups.projection": "control-projection",
	} {
		v.SetDefault(key, "agent-"+environment+"-"+suffix)
	}
	if err := v.Unmarshal(&cfg); err != nil {
		return Config{}, fmt.Errorf("decode control-plane config: %w", err)
	}
	// Viper does not decode indexed environment keys into slices automatically.
	provider := ObjectStorageProvider{Type: "s3", Bucket: "agent-artifacts", Region: "us-east-1"}
	if len(cfg.ObjectStorage.Providers) > 0 {
		provider = cfg.ObjectStorage.Providers[0]
	}
	if provider.Type == "" {
		provider.Type = "s3"
	}
	if provider.Bucket == "" {
		provider.Bucket = "agent-artifacts"
	}
	if provider.Region == "" {
		provider.Region = "us-east-1"
	}
	for key, target := range map[string]*string{
		"type": &provider.Type, "endpoint": &provider.Endpoint, "region": &provider.Region,
		"public_endpoint": &provider.PublicEndpoint,
		"bucket":          &provider.Bucket,
		"access_key":      &provider.AccessKey, "secret_key": &provider.SecretKey,
	} {
		if value := v.GetString("object_storage.providers.0." + key); value != "" {
			*target = value
		}
	}
	if len(cfg.ObjectStorage.Providers) == 0 {
		cfg.ObjectStorage.Providers = []ObjectStorageProvider{provider}
	} else {
		cfg.ObjectStorage.Providers[0] = provider
	}
	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

func (c Config) Validate() error {
	if (c.Server.Environment == "production" || c.Server.Environment == "prod") && !c.Security.AuthEnabled {
		return fmt.Errorf("production requires authentication")
	}
	if (c.Server.Environment == "production" || c.Server.Environment == "prod") && c.Security.AllowMemory {
		return fmt.Errorf("memory mode is only available for local development")
	}
	if _, _, err := net.SplitHostPort(c.Server.HTTPAddr); err != nil {
		return fmt.Errorf("server.http_addr must contain a host and port")
	}
	if c.Database.MaxConnections < 1 || c.Database.MaxConnections > math.MaxInt32 || c.Database.MinConnections < 0 || c.Database.MinConnections > c.Database.MaxConnections || c.Database.IdleTimeoutSecs < 0 {
		return fmt.Errorf("invalid database connection pool limits")
	}
	if c.Security.SessionHours < 1 {
		return fmt.Errorf("security.session_hours must be positive")
	}
	if c.MQ.Enabled && (c.MQ.Type != "rocketmq" || c.MQ.Endpoint == "") {
		return fmt.Errorf("enabled MQ requires rocketmq type and endpoint")
	}
	if c.MQ.Enabled && (c.Server.Environment == "production" || c.Server.Environment == "prod") {
		for name, value := range map[string]string{"model": c.Security.ModelEncryptionKey, "execution": c.Security.ExecutionEncryptionKey} {
			key, err := hex.DecodeString(value)
			if err != nil || len(key) != 32 {
				key, err = base64.StdEncoding.DecodeString(value)
			}
			if err != nil || len(key) != 32 {
				return fmt.Errorf("production MQ requires a 32-byte %s encryption key", name)
			}
		}
	}
	if (c.MQ.AccessKey == "") != (c.MQ.SecretKey == "") {
		return fmt.Errorf("MQ access and secret keys must be configured together")
	}
	return nil
}
