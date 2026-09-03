"""Validated runtime configuration for Cortex.

Development stays easy. Production fails during startup when a security-
critical value is missing or unsafe instead of silently inventing credentials.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import os
from urllib.parse import urlparse


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(item.strip() for item in os.environ.get(name, default).split(",") if item.strip())


@dataclass(frozen=True)
class RuntimeConfig:
    environment: str
    base_url: str
    database_url: str
    cors_origins: tuple[str, ...]
    trusted_hosts: tuple[str, ...]
    seed_sample_agents: bool
    authz_fail_closed: bool
    secure_cookies: bool
    allow_signup: bool
    bootstrap_token: str

    @property
    def production(self) -> bool:
        return self.environment == "production"


def load_runtime_config() -> RuntimeConfig:
    environment = os.environ.get("CORTEX_ENV", "development").strip().lower()
    production = environment == "production"
    base_url = os.environ.get("BASE_URL", "http://localhost:3000").rstrip("/")
    default_origins = "" if production else "http://localhost:3000,http://127.0.0.1:3000"
    host = urlparse(base_url).hostname or "localhost"
    return RuntimeConfig(
        environment=environment,
        base_url=base_url,
        database_url=os.environ.get("DATABASE_URL", "sqlite:///cortex.db"),
        cors_origins=_csv("CORS_ORIGINS", default_origins),
        trusted_hosts=_csv("TRUSTED_HOSTS", host if production else "localhost,127.0.0.1,testserver"),
        seed_sample_agents=_bool("SEED_SAMPLE_AGENTS", not production),
        authz_fail_closed=_bool("CORTEX_AUTHZ_FAIL_CLOSED", production),
        secure_cookies=_bool("SECURE_COOKIES", production),
        allow_signup=_bool("ALLOW_SIGNUP", not production),
        bootstrap_token=os.environ.get("CORTEX_BOOTSTRAP_TOKEN", ""),
    )


def production_errors(config: RuntimeConfig) -> list[str]:
    if not config.production:
        return []
    errors: list[str] = []
    secret = os.environ.get("SECRET_KEY", "")
    if len(secret) < 32 or secret == "change-me-in-production":
        errors.append("SECRET_KEY must be explicitly set to at least 32 characters")
    encryption_key = os.environ.get("CORTEX_ENCRYPTION_KEY", "").strip()
    try:
        decoded_key = base64.urlsafe_b64decode(encryption_key.encode())
    except Exception:
        decoded_key = b""
    if len(decoded_key) != 32:
        errors.append("CORTEX_ENCRYPTION_KEY must be explicitly set")
    if config.database_url.startswith("sqlite"):
        errors.append("DATABASE_URL must use PostgreSQL in production")
    if not config.base_url.startswith("https://"):
        errors.append("BASE_URL must use https:// in production")
    if not config.cors_origins or "*" in config.cors_origins:
        errors.append("CORS_ORIGINS must contain explicit trusted origins")
    if (not config.trusted_hosts or "*" in config.trusted_hosts
            or any(host in {"localhost", "127.0.0.1"} for host in config.trusted_hosts)):
        errors.append("TRUSTED_HOSTS must contain explicit hostnames")
    if config.seed_sample_agents:
        errors.append("SEED_SAMPLE_AGENTS must be false in production")
    if not config.authz_fail_closed:
        errors.append("CORTEX_AUTHZ_FAIL_CLOSED must be true in production")
    if config.allow_signup:
        errors.append("ALLOW_SIGNUP must be false in production")
    if len(config.bootstrap_token) < 32:
        errors.append("CORTEX_BOOTSTRAP_TOKEN must be explicitly set to at least 32 characters")
    return errors


def validate_runtime_config(config: RuntimeConfig) -> None:
    errors = production_errors(config)
    if errors:
        raise RuntimeError("Unsafe Cortex production configuration:\n- " + "\n- ".join(errors))
