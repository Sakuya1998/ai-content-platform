"""Runtime-only configuration loaded from RUNTIME_* environment variables."""

from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RUNTIME_",
        env_file=".env.runtime",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    service_name: str = "runtime"
    environment: str = Field(default="dev", pattern=r"^[a-z][a-z0-9-]{0,31}$")
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    database_url: str = ""
    redis_url: str = ""
    redis_key_prefix: str = "runtime:"
    rocketmq_endpoint: str = ""
    rocketmq_access_key: SecretStr = SecretStr("")
    rocketmq_secret_key: SecretStr = SecretStr("")
    rocketmq_namespace: str = ""
    rocketmq_tls_enabled: bool = False
    rocketmq_task_topic: str = ""
    rocketmq_control_topic: str = ""
    rocketmq_event_topic: str = ""
    rocketmq_memory_topic: str = ""
    rocketmq_dispatch_group: str = ""
    rocketmq_control_group: str = ""
    rocketmq_memory_group: str = ""
    rocketmq_memory_scheduler_group: str = ""
    worker_handler: str = ""
    memory_handler: str = ""
    worker_max_attempts: int = Field(default=3, ge=1, le=20)
    worker_retry_seconds: int = Field(default=30, ge=1)
    llm_provider: Literal["openai", "openai_compatible", "anthropic"] = "openai_compatible"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    analysis_model: str = ""
    llm_max_tokens: int = Field(default=4096, ge=128, le=65536)
    llm_timeout_seconds: float = Field(default=120, gt=0)
    media_poll_seconds: float = Field(default=5, gt=0, le=60)
    media_poll_timeout_seconds: float = Field(default=3600, gt=0, le=86400)
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    compose_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    compose_threads: int = Field(default=2, ge=1, le=16)
    compose_input_max_bytes: int = Field(default=268435456, ge=1)
    max_project_tokens: int = Field(default=0, ge=0)
    embedding_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = SecretStr("")
    execution_encryption_key: SecretStr = SecretStr("")
    context_token_budget: int = Field(default=12000, ge=100)
    object_storage_endpoint: str = ""
    object_storage_bucket: str = "agent-artifacts"
    object_storage_region: str = "us-east-1"
    artifact_max_bytes: int = Field(default=52428800, ge=1)
    object_storage_access_key: str = ""
    object_storage_secret_key: SecretStr = SecretStr("")
    worker_id: str = "runtime-local"
    job_lease_seconds: int = Field(default=60, ge=1, le=3600)
    job_heartbeat_seconds: int = Field(default=15, ge=1, le=1800)

    @model_validator(mode="after")
    def messaging_defaults(self):
        if self.job_heartbeat_seconds >= self.job_lease_seconds:
            raise ValueError("job_heartbeat_seconds must be smaller than job_lease_seconds")
        for field, suffix in {
            "rocketmq_task_topic": "runtime-tasks",
            "rocketmq_control_topic": "runtime-control",
            "rocketmq_event_topic": "runtime-events",
            "rocketmq_memory_topic": "memory-tasks",
            "rocketmq_dispatch_group": "runtime-dispatch",
            "rocketmq_control_group": "runtime-control",
            "rocketmq_memory_group": "memory-worker",
            "rocketmq_memory_scheduler_group": "memory-scheduler",
        }.items():
            if not getattr(self, field):
                setattr(self, field, f"agent-{self.environment}-{suffix}")
        return self

    @property
    def configured_database(self) -> bool:
        return bool(self.database_url.strip())
