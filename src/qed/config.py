"""Runtime configuration, read once from the environment.

Every knob has a safe default so `qed dev` works on a fresh clone with no
cloud account. Cloud features (Bedrock, Guardrails, S3 backups) switch on
only when their variables are present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw and raw.strip() else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw and raw.strip() else default


@dataclass(frozen=True)
class Settings:
    # Network
    host: str = field(default_factory=lambda: os.environ.get("QED_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("QED_PORT", 8000))
    partners_port: int = field(default_factory=lambda: _int("QED_PARTNERS_PORT", 9100))
    partners_url: str = field(
        default_factory=lambda: os.environ.get(
            "QED_PARTNERS_URL", f"http://127.0.0.1:{_int('QED_PARTNERS_PORT', 9100)}"
        )
    )
    public_url: str = field(default_factory=lambda: os.environ.get("QED_PUBLIC_URL", ""))
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in os.environ.get(
                "QED_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173"
            ).split(",")
            if o.strip()
        )
    )

    # Security
    dev_mode: bool = field(default_factory=lambda: _bool("QED_DEV_MODE", True))
    auth_enabled: bool = field(default_factory=lambda: _bool("QED_AUTH", True))
    jwt_secret: str = field(default_factory=lambda: os.environ.get("QED_JWT_SECRET", ""))
    alexa_compat_401: bool = field(default_factory=lambda: _bool("QED_ALEXA_COMPAT_401", False))
    rate_limit_per_minute: int = field(default_factory=lambda: _int("QED_RATE_LIMIT_PER_MINUTE", 240))
    max_request_chars: int = field(default_factory=lambda: _int("QED_MAX_REQUEST_CHARS", 1200))
    max_booking_usd: int = field(default_factory=lambda: _int("QED_MAX_BOOKING_USD", 15000))

    # Engine
    solver_time_limit_s: float = field(default_factory=lambda: _float("QED_SOLVER_TIME_LIMIT", 2.0))
    solver_workers: int = field(default_factory=lambda: _int("QED_SOLVER_WORKERS", 8))
    latency_budget_ms: int = field(default_factory=lambda: _int("QED_LATENCY_BUDGET_MS", 500))
    z3_crosscheck: bool = field(default_factory=lambda: _bool("QED_Z3", True))
    partner_timeout_s: float = field(default_factory=lambda: _float("QED_PARTNER_TIMEOUT", 1.5))
    cache_ttl_s: float = field(default_factory=lambda: _float("QED_CACHE_TTL", 120.0))

    # LLM (Amazon Bedrock). Off unless explicitly enabled.
    llm_mode: str = field(default_factory=lambda: os.environ.get("QED_LLM", "off"))  # off | bedrock
    bedrock_region: str = field(default_factory=lambda: os.environ.get("AWS_REGION", "us-east-1"))
    bedrock_parser_model: str = field(
        default_factory=lambda: os.environ.get("QED_PARSER_MODEL", "us.amazon.nova-lite-v1:0")
    )
    bedrock_explainer_model: str = field(
        default_factory=lambda: os.environ.get("QED_EXPLAINER_MODEL", "us.amazon.nova-pro-v1:0")
    )
    guardrail_id: str = field(default_factory=lambda: os.environ.get("QED_GUARDRAIL_ID", ""))
    guardrail_version: str = field(default_factory=lambda: os.environ.get("QED_GUARDRAIL_VERSION", "DRAFT"))
    parse_coverage_threshold: float = field(default_factory=lambda: _float("QED_PARSE_COVERAGE", 0.92))

    # State, backups, observability
    state_dir: Path = field(default_factory=lambda: Path(os.environ.get("QED_STATE_DIR", "var")))
    backup_every: int = field(default_factory=lambda: _int("QED_BACKUP_EVERY", 25))
    backup_s3_bucket: str = field(default_factory=lambda: os.environ.get("QED_BACKUP_S3_BUCKET", ""))
    otel_console: bool = field(default_factory=lambda: _bool("QED_OTEL_CONSOLE", False))
    log_json: bool = field(default_factory=lambda: _bool("QED_LOG_JSON", False))

    @property
    def resource_url(self) -> str:
        return (self.public_url or f"http://{self.host}:{self.port}").rstrip("/") + "/mcp"

    @property
    def llm_enabled(self) -> bool:
        return self.llm_mode.lower() == "bedrock"


def load() -> Settings:
    return Settings()
