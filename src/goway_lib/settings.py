from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    SERVICE_NAME: str
    ENV: str = "dev"
    DATABASE_URL: str
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
