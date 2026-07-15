# -*- coding: utf-8 -*-
"""
pinterest_engine.publisher
---------------------------
Pinterest API v5 pin creation -- Standard (Production) access.

Reads from .env:
    PINTEREST_ACCESS_TOKEN   -- Bearer token (required)
    PINTEREST_BOARD_ID       -- Target board numeric ID (required)
    PINTEREST_REFRESH_TOKEN  -- Used to auto-refresh on 401 (optional but recommended)
    PINTEREST_APP_ID         -- App ID for token refresh (optional)
    PINTEREST_APP_SECRET     -- App Secret for token refresh (optional)

Token refresh:
    On HTTP 401, the publisher automatically attempts to exchange
    PINTEREST_REFRESH_TOKEN for a fresh access token and writes it
    back to .env before retrying the original request once.

Error handling:
    401  -> auto-refresh attempted; if refresh fails, raises PinterestTokenExpiredError
    429  -> exponential backoff, up to _MAX_RETRIES attempts
    other -> logged, retried up to _MAX_RETRIES times, then returns None
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_API_BASE      = "https://api.pinterest.com/v5"
_PINS_ENDPOINT = f"{_API_BASE}/pins"
_TOKEN_URL     = "https://api.pinterest.com/v5/oauth/token"
_SALES_URL     = "http://blueprint.holisticprotocolslab.com/"
_HASHTAGS      = (
    "#NaturalHealth #HolisticProtocol #CellularHealing "
    "#NaturalRemedies #HolisticLiving"
)
_MAX_RETRIES       = 3
_BACKOFF_BASE_SEC  = 10

_ENV_PATH = (Path(__file__).resolve().parents[1] / ".env")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PinterestTokenExpiredError(Exception):
    """Raised when the Pinterest API returns 401 and auto-refresh also fails."""


class PinterestPublishError(Exception):
    """Raised for unrecoverable publish failures."""


# ---------------------------------------------------------------------------
# .env writer (used by token-refresh)
# ---------------------------------------------------------------------------

def _write_env_key(key: str, value: str) -> None:
    """Update or append key=value in .env without touching other lines."""
    if not _ENV_PATH.is_file():
        return
    text = _ENV_PATH.read_text(encoding="utf-8")
    pattern = rf"^{re.escape(key)}\s*=.*$"
    new_line = f"{key}={value}"
    if re.search(pattern, text, re.MULTILINE):
        text = re.sub(pattern, new_line, text, flags=re.MULTILINE)
    else:
        text = text.rstrip("\n") + f"\n{new_line}\n"
    _ENV_PATH.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# PinterestPublisher
# ---------------------------------------------------------------------------

class PinterestPublisher:
    """
    Publishes a single pin to Pinterest via the v5 API (Standard access).

    Auto-refreshes the access token on 401 using the stored refresh_token,
    then retries the request once before raising PinterestTokenExpiredError.
    """

    def __init__(
        self,
        access_token: str | None = None,
        board_id: str | None = None,
    ) -> None:
        import sys  # noqa: PLC0415
        _root = Path(__file__).resolve().parents[1]
        if str(_root) not in sys.path:
            sys.path.insert(0, str(_root))

        self.token: str = access_token or os.getenv("PINTEREST_ACCESS_TOKEN", "")
        self.board_id: str = board_id or os.getenv("PINTEREST_BOARD_ID", "")
        self._refreshed_this_session = False

        if not self.token:
            raise ValueError(
                "PINTEREST_ACCESS_TOKEN is not set. Add it to .env."
            )
        if not self.board_id:
            raise ValueError(
                "PINTEREST_BOARD_ID is not set. Add it to .env."
            )

    # ------------------------------------------------------------------
    # Public

    def publish(self, record: dict, pin_image_bytes: bytes) -> dict | None:
        """
        Publish a pin. Guards against double-posting and unsafe captions.
        Returns the Pinterest API response dict on success, None on soft failure.
        Raises PinterestTokenExpiredError if token is dead and refresh fails.
        """
        # Guard: never double-post
        if record.get("publication_status", {}).get("posted_on_pinterest"):
            log.warning(
                "Skipping already-posted entry: %s (pin_id=%s)",
                record.get("post_id", "?"),
                record.get("publication_status", {}).get("pinterest_pin_id"),
            )
            return None

        # Guard: caption safety
        caption = record.get("pinterest_caption", "")
        from pinterest_engine.inventory import (  # noqa: PLC0415
            build_caption_regex, validate_caption_safe,
        )
        valid, reason = validate_caption_safe(caption)
        if not valid:
            log.warning("Caption safety fix (%s) for %s.", reason, record.get("post_id", "?"))
            caption = build_caption_regex(
                record.get("original_caption", ""), record.get("variant_index", 0)
            )
            record["pinterest_caption"] = caption

        title = (record.get("pinterest_title") or record.get("topic", "Holistic Protocol"))[:100]
        payload = {
            "board_id": self.board_id,
            "title": title,
            "description": self._build_description(record),
            "link": _SALES_URL,
            "media_source": {
                "source_type": "image_base64",
                "content_type": "image/jpeg",
                "data": base64.b64encode(pin_image_bytes).decode("ascii"),
            },
        }

        log.info(
            "Publishing: '%s' (board=%s, image=%d KB)",
            title, self.board_id, len(pin_image_bytes) // 1024,
        )
        return self._post_with_retry(payload)

    def validate_token(self) -> bool:
        """
        Check token validity via GET /v5/boards (boards:read scope only).
        Returns True if valid, False otherwise. Does not raise.
        """
        try:
            resp = requests.get(
                f"{_API_BASE}/boards",
                headers=self._headers(),
                params={"page_size": 1},
                timeout=15,
            )
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                name = items[0].get("name", "?") if items else "(no boards)"
                log.info("Token valid. First board: '%s'", name)
                return True
            if resp.status_code == 401:
                log.warning("Token 401 on validation. Attempting auto-refresh...")
                if self._try_refresh():
                    return self.validate_token()
                log.error("Token invalid and refresh failed.")
                return False
            log.warning("Validation HTTP %d: %s", resp.status_code, resp.text[:150])
            return False
        except requests.RequestException as exc:
            log.error("Token validation error: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Private

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def _build_description(self, record: dict) -> str:
        """Pinterest pin description: caption + hashtags, max 500 chars total."""
        caption = record.get("pinterest_caption") or record.get("humanized_caption", "")
        max_caption = 500 - len(_HASHTAGS) - 2
        if len(caption) > max_caption:
            caption = caption[:max_caption - 3].rsplit(" ", 1)[0] + "..."
        return f"{caption}\n\n{_HASHTAGS}"

    def _try_refresh(self) -> bool:
        """
        Exchange PINTEREST_REFRESH_TOKEN for a new access token.
        On success: updates self.token and writes new token to .env.
        Returns True on success, False on failure.
        """
        if self._refreshed_this_session:
            log.warning("Already refreshed once this session; not retrying.")
            return False

        refresh_token = os.getenv("PINTEREST_REFRESH_TOKEN", "")
        app_id        = os.getenv("PINTEREST_APP_ID", "")
        app_secret    = os.getenv("PINTEREST_APP_SECRET", "")

        if not all([refresh_token, app_id, app_secret]):
            log.warning(
                "Auto-refresh skipped: PINTEREST_REFRESH_TOKEN, "
                "PINTEREST_APP_ID, or PINTEREST_APP_SECRET missing in .env."
            )
            return False

        creds = base64.b64encode(f"{app_id}:{app_secret}".encode()).decode()
        body = urllib.parse.urlencode({
            "grant_type":    "refresh_token",
            "refresh_token": refresh_token,
        }).encode()
        req = urllib.request.Request(
            _TOKEN_URL,
            data=body,
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type":  "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            log.error("Token refresh request failed: %s", exc)
            return False

        new_token = result.get("access_token", "")
        if not new_token:
            log.error("Refresh response missing access_token: %s", result)
            return False

        self.token = new_token
        self._refreshed_this_session = True
        _write_env_key("PINTEREST_ACCESS_TOKEN", new_token)
        if result.get("refresh_token"):
            _write_env_key("PINTEREST_REFRESH_TOKEN", result["refresh_token"])

        log.info("Access token refreshed and saved to .env.")
        return True

    def _post_with_retry(self, payload: dict) -> dict | None:
        """POST with exponential backoff on 429; auto-refresh on 401."""
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    _PINS_ENDPOINT,
                    headers=self._headers(),
                    data=json.dumps(payload),
                    timeout=60,
                )
            except requests.RequestException as exc:
                log.error("Network error attempt %d/%d: %s", attempt, _MAX_RETRIES, exc)
                if attempt < _MAX_RETRIES:
                    time.sleep(_BACKOFF_BASE_SEC * attempt)
                    continue
                return None

            if resp.status_code == 201:
                data = resp.json()
                log.info("Pin created! ID=%s", data.get("id", "?"))
                return data

            if resp.status_code == 401:
                log.warning("401 Unauthorized. Attempting token refresh...")
                if self._try_refresh():
                    log.info("Refresh succeeded. Retrying publish...")
                    continue   # retry with new token
                raise PinterestTokenExpiredError(
                    "Pinterest access token expired and refresh failed. "
                    "Run: python pinterest_oauth.py to re-authenticate."
                )

            if resp.status_code == 429:
                wait = _BACKOFF_BASE_SEC * (2 ** attempt)
                log.warning("Rate limited (429). Waiting %ds...", wait)
                time.sleep(wait)
                continue

            log.error(
                "Pinterest API HTTP %d attempt %d/%d: %s",
                resp.status_code, attempt, _MAX_RETRIES,
                resp.text[:400],
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_BACKOFF_BASE_SEC)
                continue
            return None

        return None
