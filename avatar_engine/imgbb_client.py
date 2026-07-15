# -*- coding: utf-8 -*-
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_IMGBB_ENDPOINT = "https://api.imgbb.com/1/upload"


def upload_image_file_to_imgbb(
    api_key: str,
    image_path: Path,
    *,
    timeout_s: float = 180.0,
) -> str | None:
    """
    ImgBB REST upload (multipart-style form encoded body).

    Returns the public HTTPS URL (`display_url`, else `url`) or ``None`` on failure.
    """
    raw_key = (api_key or "").strip()
    ip = Path(image_path).expanduser().resolve()
    if not raw_key or not ip.is_file():
        return None

    payload = urllib.parse.urlencode(
        {"key": raw_key, "image": base64.b64encode(ip.read_bytes()).decode("ascii")},
    ).encode("utf-8")

    req = urllib.request.Request(
        _IMGBB_ENDPOINT,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.HTTPError, urllib.error.URLError) as exc:  # noqa: PERF203
        logger.warning("ImgBB upload failed (%s): %s", ip.name, exc)
        return None

    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("ImgBB non-JSON response for %s (first bytes): %s", ip.name, body[:400])
        return None

    if not decoded.get("success"):
        logger.warning(
            "ImgBB reported failure for %s: status=%s err=%s",
            ip.name,
            decoded.get("status"),
            decoded.get("error") or decoded,
        )
        return None

    data = decoded.get("data") or {}
    url = ((data.get("display_url") or data.get("url") or "").strip())
    return url or None
