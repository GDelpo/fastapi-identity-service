"""Application settings and configuration."""

from pydantic import AliasChoices, Field, SecretStr, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    This configuration is infrastructure-agnostic. The application runs
    the same way locally or behind any reverse proxy (Traefik, Nginx, etc.).
    Proxy-related configuration (path prefixes, routing) belongs in the
    infrastructure layer, not here.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # =========================================================================
    # Environment
    # =========================================================================
    debug: bool = False
    # environment: derived from debug in validator if not set
    environment: str | None = None

    # =========================================================================
    # Logging
    # =========================================================================
    # log_level: derived from debug in validator if not set
    log_level: str | None = None
    log_format: str = "json"  # json or text
    log_file: str | None = None  # If set, logs to file + stdout

    # =========================================================================
    # Application Metadata
    # =========================================================================
    project_name: str = "Identity Service"
    project_version: str = "1.0.0"
    project_description: str = "Authentication and user management microservice"

    # Internal API prefix - this is the technical prefix for all routes
    # The proxy (if any) handles external path mapping
    api_prefix: str = "/api/v1"

    # =========================================================================
    # CORS
    # =========================================================================
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])

    # =========================================================================
    # Database (PostgreSQL)
    # =========================================================================
    db_host: str = "localhost"
    db_port: int = 5432
    db_user: str = "identity_user"
    db_password: SecretStr = SecretStr("identity_password")
    db_name: str = "identity_db"

    @computed_field
    @property
    def database_url(self) -> str:
        """Construct async database URL."""
        return (
            f"postgresql+asyncpg://{self.db_user}:"
            f"{self.db_password.get_secret_value()}@"
            f"{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # =========================================================================
    # Redis
    # =========================================================================
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: SecretStr | None = None

    @computed_field
    @property
    def redis_url(self) -> str:
        """Construct Redis URL."""
        if self.redis_password:
            return (
                f"redis://:{self.redis_password.get_secret_value()}@"
                f"{self.redis_host}:{self.redis_port}/{self.redis_db}"
            )
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # =========================================================================
    # Security
    # =========================================================================
    max_login_attempts: int = 5
    lockout_duration_minutes: int = 15

    # =========================================================================
    # JWT
    # =========================================================================
    secret_key: SecretStr
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    # =========================================================================
    # LDAP (Optional)
    # =========================================================================
    ldap_url: str = ""
    ldap_admin_username: SecretStr | None = None
    ldap_admin_password: SecretStr | None = None
    ldap_search_bases: list[str] = Field(default_factory=list)
    ldap_ou_to_avoid: list[str] = Field(default_factory=list)
    ldap_attr_to_retrieve: list[str] = Field(default_factory=list)

    @property
    def ldap_enabled(self) -> bool:
        """Check if LDAP is configured."""
        return bool(self.ldap_url)

    # =========================================================================
    # Email Service Integration
    # =========================================================================
    # Full URL to the email service's emails endpoint (no path appended by client)
    # Examples:
    #   Local:  http://localhost:8081/api/v1/emails
    #   Docker: http://email_service_api:8081/api/v1/emails
    # MAILSENDER_URL is still read for older deployments.
    email_service_url: str = Field(
        default="http://localhost:8081/api/v1/emails",
        validation_alias=AliasChoices("EMAIL_SERVICE_URL", "MAILSENDER_URL"),
    )
    email_service_timeout: int = Field(
        default=30,
        validation_alias=AliasChoices("EMAIL_SERVICE_TIMEOUT", "MAILSENDER_TIMEOUT"),
    )

    # Password Reset & Email Verification
    password_reset_token_expire_minutes: int = 60
    email_verification_token_expire_minutes: int = 1440  # 24 hours

    # Admin seed: ADMIN_USER / ADMIN_PASSWORD / ADMIN_EMAIL are read by the
    # seed_admin_user migration only, with no defaults (see README).

    # =========================================================================
    # Validation
    # =========================================================================
    @model_validator(mode="after")
    def validate_settings(self):
        """Validate settings and emit warnings for insecure configurations."""
        import warnings

        # Derive environment from debug if not set
        if self.environment is None:
            object.__setattr__(self, "environment", "development" if self.debug else "production")

        # Derive log_level from debug if not set
        if self.log_level is None:
            object.__setattr__(self, "log_level", "DEBUG" if self.debug else "INFO")
        else:
            object.__setattr__(self, "log_level", self.log_level.upper())

        # Ensure api_prefix starts with /
        if not self.api_prefix.startswith("/"):
            object.__setattr__(self, "api_prefix", f"/{self.api_prefix}")

        # Security warnings for production
        if not self.debug:
            secret_value = self.secret_key.get_secret_value()
            insecure_keys = [
                "dev-secret-key",
                "your-secret-key",
                "change-this",
                "secret",
                "password",
            ]

            if any(insecure in secret_value.lower() for insecure in insecure_keys):
                warnings.warn(
                    "🔴 SECURITY WARNING: SECRET_KEY appears insecure for production.",
                    stacklevel=2,
                )

            if len(secret_value) < 32:
                warnings.warn(
                    f"🔴 SECURITY WARNING: SECRET_KEY too short ({len(secret_value)} chars).",
                    stacklevel=2,
                )

            if "*" in self.cors_origins:
                warnings.warn(
                    "🔴 SECURITY WARNING: CORS allows all origins (*) in production.",
                    stacklevel=2,
                )

        return self


# Global settings instance
settings = Settings()
