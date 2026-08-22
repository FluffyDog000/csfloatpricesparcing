"""Item image resolution via CSFloat's official listings endpoint.

GET /api/v1/listings?market_hash_name=... (needs CSFLOAT_API_KEY) returns
active listings; each listing's item.icon_url is a Steam economy image hash.
The full image URL is the configured Steam CDN prefix + that hash.

Results are cached in items.icon_url so the API is hit at most once per
refresh_days per item. If no API key is configured or the fetch fails, the URL
is left as None and the dashboard shows a placeholder — never an error.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import requests

from .config import AppConfig
from .db import Database

log = logging.getLogger("csfloat.images")


class _RateLimited(Exception):
    """Official listings API returned 429 — transient, don't cache the miss."""


class ImageService:
    def __init__(self, config: AppConfig, db: Database, client: object | None = None):
        self.config = config
        self.db = db
        # Optional CSFloatClient; if given, image requests share its global
        # request spacing so they can't burst the official API into a 429.
        self.client = client

    def _needs_refresh(self, cached: dict | None) -> bool:
        if not cached:
            return True
        cached_at = cached.get("image_cached_at")
        if not cached_at:
            return True
        try:
            when = datetime.fromisoformat(cached_at)
        except ValueError:
            return True
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - when
        if not cached.get("icon_url"):
            # Previous fetch produced no URL (e.g. no API key yet, or a transient
            # failure). Retry soon rather than waiting the full refresh window,
            # so images appear shortly after an API key is added.
            return age > timedelta(hours=1)
        return age > timedelta(days=self.config.images.refresh_days)

    def _build_url(self, icon_url: str) -> str:
        if icon_url.startswith("http://") or icon_url.startswith("https://"):
            return icon_url
        return self.config.images.steam_cdn_prefix.rstrip("/") + "/" + icon_url

    def _fetch_icon_url(self, market_hash_name: str) -> str | None:
        api_key = self.config.http.api_key
        if not api_key:
            return None
        url = self.config.http.base_url + "/api/v1/listings"
        headers = {
            "Authorization": api_key,
            "User-Agent": self.config.http.user_agent,
            "Accept": "application/json",
        }
        try:
            resp = requests.get(
                url,
                params={"market_hash_name": market_hash_name, "limit": 1},
                headers=headers,
                timeout=self.config.http.timeout_seconds,
            )
            if resp.status_code == 429:
                raise _RateLimited()
            resp.raise_for_status()
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("Image fetch failed for '%s': %s", market_hash_name, exc)
            return None

        listings = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(listings, list) or not listings:
            return None
        first = listings[0]
        icon = None
        if isinstance(first, dict):
            item = first.get("item") or {}
            icon = item.get("icon_url") or first.get("icon_url")
        if not icon:
            return None
        return self._build_url(str(icon))

    def get_or_fetch(self, market_hash_name: str) -> str | None:
        """Return a cached image URL, fetching + caching if stale/missing.
        On a 429 the miss is NOT cached, so it is retried on the next pass."""
        cached = self.db.get_icon(market_hash_name)
        if not self._needs_refresh(cached):
            return cached.get("icon_url") if cached else None

        # Share the collector's global request spacing to avoid bursts → 429.
        if self.client is not None and hasattr(self.client, "_respect_spacing"):
            self.client._respect_spacing()

        try:
            url = self._fetch_icon_url(market_hash_name)
        except _RateLimited:
            log.warning("Image fetch rate-limited (429) for '%s'; will retry later",
                        market_hash_name)
            return cached.get("icon_url") if cached else None

        # Cache the result (even None marks "tried at this time" via timestamp).
        self.db.set_icon(market_hash_name, url)
        if url:
            log.info("Cached image URL for '%s'", market_hash_name)
        return url
