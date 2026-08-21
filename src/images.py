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


class ImageService:
    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db

    def _needs_refresh(self, cached: dict | None) -> bool:
        if not cached:
            return True
        if not cached.get("icon_url"):
            # No URL cached yet, or a previous fetch failed — retry, but only
            # once past the refresh window (avoid hammering on every load).
            cached_at = cached.get("image_cached_at")
            if not cached_at:
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
        """Return a cached image URL, fetching + caching if stale/missing."""
        cached = self.db.get_icon(market_hash_name)
        if not self._needs_refresh(cached):
            return cached.get("icon_url") if cached else None

        url = self._fetch_icon_url(market_hash_name)
        # Cache the result (even None marks "tried at this time" via timestamp).
        self.db.set_icon(market_hash_name, url)
        if url:
            log.info("Cached image URL for '%s'", market_hash_name)
        return url
