"""Configuration loading: merges .env (secrets) with config.yaml (behaviour)
and items.yaml (tracked items).

Secrets ONLY come from environment / .env — never hard-coded, never from YAML.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Project root = parent of the "src" directory.
ROOT = Path(__file__).resolve().parent.parent

# Load .env once at import time (does not override real env vars already set).
load_dotenv(ROOT / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    return val.strip()


def _abs(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (ROOT / p)


@dataclass
class HttpConfig:
    base_url: str
    sales_path_template: str
    timeout_seconds: int
    user_agent: str
    cookie: str | None
    authorization: str | None
    api_key: str | None


@dataclass
class RateLimitConfig:
    base_backoff_seconds: float
    max_backoff_seconds: float
    max_retries: int


@dataclass
class PollingConfig:
    interval_min_minutes: float
    interval_max_minutes: float
    min_seconds_between_requests: float
    gap_warning_min_overlap: int
    rate_limit: RateLimitConfig


@dataclass
class ReportingConfig:
    float_bucket_size: float
    default_period: str


@dataclass
class WebConfig:
    host: str
    port: int


@dataclass
class ImagesConfig:
    steam_cdn_prefix: str
    refresh_days: int


@dataclass
class ItemConfig:
    name: str
    active: bool = True
    interval_min_minutes: float | None = None
    interval_max_minutes: float | None = None
    # Whether the paint seed / pattern matters for this skin's price.
    # Defaults to True (assume pattern matters until explicitly disabled).
    # Only affects display / future logic, never data collection.
    pattern_sensitive: bool = True


@dataclass
class AppConfig:
    http: HttpConfig
    polling: PollingConfig
    reporting: ReportingConfig
    web: WebConfig
    images: ImagesConfig
    db_path: Path
    log_path: Path
    raw_dump_dir: Path
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(config_path: Path | None = None) -> AppConfig:
    """Load and merge configuration. Env values take precedence over YAML for
    the few keys that can be overridden by env (paths, poll bounds)."""
    cfg = _load_yaml(config_path or (ROOT / "config.yaml"))

    poll = cfg.get("polling", {})
    rl = poll.get("rate_limit", {})
    http = cfg.get("http", {})
    rep = cfg.get("reporting", {})
    web = cfg.get("web", {})
    images = cfg.get("images", {})

    # Poll bounds: env overrides YAML if present.
    imin = float(_env("CSFLOAT_POLL_MIN_MINUTES") or poll.get("interval_min_minutes", 15))
    imax = float(_env("CSFLOAT_POLL_MAX_MINUTES") or poll.get("interval_max_minutes", 30))

    return AppConfig(
        http=HttpConfig(
            base_url=http.get("base_url", "https://csfloat.com").rstrip("/"),
            sales_path_template=http.get(
                "sales_path_template", "/api/v1/history/{name}/sales"
            ),
            timeout_seconds=int(http.get("timeout_seconds", 20)),
            user_agent=" ".join(str(http.get("user_agent", "")).split())
            or "Mozilla/5.0",
            cookie=_env("CSFLOAT_COOKIE"),
            authorization=_env("CSFLOAT_AUTHORIZATION"),
            api_key=_env("CSFLOAT_API_KEY"),
        ),
        polling=PollingConfig(
            interval_min_minutes=imin,
            interval_max_minutes=imax,
            min_seconds_between_requests=float(
                poll.get("min_seconds_between_requests", 1.2)
            ),
            gap_warning_min_overlap=int(poll.get("gap_warning_min_overlap", 5)),
            rate_limit=RateLimitConfig(
                base_backoff_seconds=float(rl.get("base_backoff_seconds", 5)),
                max_backoff_seconds=float(rl.get("max_backoff_seconds", 300)),
                max_retries=int(rl.get("max_retries", 5)),
            ),
        ),
        reporting=ReportingConfig(
            float_bucket_size=float(rep.get("float_bucket_size", 0.01)),
            default_period=str(rep.get("default_period", "7d")),
        ),
        web=WebConfig(
            host=str(_env("CSFLOAT_WEB_HOST") or web.get("host", "127.0.0.1")),
            port=int(_env("CSFLOAT_WEB_PORT") or web.get("port", 5000)),
        ),
        images=ImagesConfig(
            steam_cdn_prefix=str(
                images.get(
                    "steam_cdn_prefix",
                    "https://community.cloudflare.steamstatic.com/economy/image/",
                )
            ),
            refresh_days=int(images.get("refresh_days", 7)),
        ),
        db_path=_abs(_env("CSFLOAT_DB_PATH", "data/csfloat_sales.db")),
        log_path=_abs(_env("CSFLOAT_LOG_PATH", "logs/collector.log")),
        raw_dump_dir=_abs(_env("CSFLOAT_RAW_DUMP_DIR", "raw_dumps")),
        _raw=cfg,
    )


def load_items(items_path: Path | None = None) -> list[ItemConfig]:
    """Load the tracked-items list from items.yaml."""
    data = _load_yaml(items_path or (ROOT / "items.yaml"))
    raw_items = data.get("items", []) or []
    result: list[ItemConfig] = []
    for entry in raw_items:
        if isinstance(entry, str):
            result.append(ItemConfig(name=entry))
            continue
        if not isinstance(entry, dict):
            continue
        # Accept either "name" or "market_hash_name" as the item key.
        name = entry.get("name") or entry.get("market_hash_name")
        if not name:
            continue
        result.append(
            ItemConfig(
                name=str(name),
                active=bool(entry.get("active", True)),
                interval_min_minutes=entry.get("interval_min_minutes"),
                interval_max_minutes=entry.get("interval_max_minutes"),
                # Default True when the field is omitted.
                pattern_sensitive=bool(entry.get("pattern_sensitive", True)),
            )
        )
    return result
