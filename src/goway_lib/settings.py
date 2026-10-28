from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    SERVICE_NAME: str
    ENV: str = "dev"
    DATABASE_URL: str
    # The process's share of Postgres connections: it keeps `pool_size` open and borrows up to `max_overflow` more
    # under load, waiting `DB_POOL_TIMEOUT_SECONDS` for one before failing. A worker needs fewer than the API. The
    # sum of size + overflow over every process on a database must stay under its `max_connections`.
    DB_POOL_SIZE: int = 4
    DB_MAX_OVERFLOW: int = 4
    DB_WORKER_POOL_SIZE: int = 2
    DB_WORKER_MAX_OVERFLOW: int = 2
    DB_POOL_TIMEOUT_SECONDS: float = 10
    DB_POOL_RECYCLE_SECONDS: int = 1800
    REDIS_URL: str = "redis://localhost:6379/0"
    NATS_URL: str = "nats://localhost:4222"
    GLITCHTIP_DSN: str = ""
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    LOG_LEVEL: str = "INFO"
    S3_ENDPOINT: str = "http://localhost:8333"
    S3_PUBLIC_ENDPOINT: str = ""
    S3_PUBLIC_BASE_URL: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = "us-east-1"
    S3_BUCKET_PRIVATE: str = "goway-private"
    S3_BUCKET_MEDIA: str = "goway-media"
    S3_BUCKET_PUBLIC: str = "goway-public"
    S3_CORS_ORIGINS: list[str] = []
