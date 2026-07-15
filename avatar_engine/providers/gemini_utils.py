# -*- coding: utf-8 -*-
"""
Gemini model utilities -- dynamic discovery, validated chains, resilient generation.

Architecture
------------
* Handshake-first: get_active_model_id() queries models.list() BEFORE any
  generateContent call and returns only live, confirmed model IDs.
* Endpoint fallback: make_gemini_client_with_fallback() tries v1beta then v1.
* Validated chains: build_model_chain() includes ONLY models confirmed by
  models.list(). If a model is absent from the live list, it is skipped and the
  available options are logged. Hardcoded fallbacks are a last-resort only when
  models.list() returns nothing at all (e.g. network failure at init time).
* Retry policy: 503/UNAVAILABLE -> exponential backoff (2^n s, max 3 attempts).
  404/NOT_FOUND -> advance chain immediately.

REST base (v1beta default):
  https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal

from google import genai
from google.genai import errors as genai_errors

logger = logging.getLogger(__name__)

Capability = Literal["text", "image"]

# Validated GA defaults -- used ONLY when models.list() returns nothing.
_LAST_RESORT_TEXT = [
    "models/gemini-2.5-flash",        # preferred: fastest verified model
    "models/gemini-2.5-pro",          # first fallback
    "models/gemini-1.5-flash-latest",
    "models/gemini-1.5-pro-latest",
    "models/gemini-1.5-flash-002",
    "models/gemini-1.5-pro-002",
]

_LAST_RESORT_IMAGE = [
    "models/gemini-3-pro-image-preview",
    "models/gemini-2.5-flash-image",
    "models/imagen-3.0-generate-002",
]

# Keep these for config-level references.
STABLE_TEXT_MODEL = "models/gemini-2.5-flash"
UNIVERSAL_TEXT_FALLBACKS = _LAST_RESORT_TEXT
UNIVERSAL_IMAGE_FALLBACKS = _LAST_RESORT_IMAGE

# Per-model 503 retry budget.
MAX_503_RETRIES = 3


# ---------------------------------------------------------------------------
# Client factory -- v1beta with v1 fallback
# ---------------------------------------------------------------------------

def make_gemini_client(api_key: str, *, api_version: str = "v1beta") -> genai.Client:
    """
    Return a google-genai Client pinned to ``api_version``.
    The SDK sends ``x-goog-api-key`` automatically.
    """
    return genai.Client(
        api_key=api_key,
        http_options={"api_version": api_version},
    )


def make_gemini_client_with_fallback(api_key: str) -> genai.Client:
    """
    Try v1beta first (preferred). If models.list() returns nothing or raises,
    silently fall back to the stable v1 endpoint.
    """
    for version in ("v1beta", "v1"):
        try:
            client = make_gemini_client(api_key, api_version=version)
            rows = list(client.models.list())
            if rows:
                logger.info(
                    "Gemini client connected via %s (%d models available).", version, len(rows)
                )
                return client
            logger.warning("Gemini %s: models.list() returned empty; trying next version.", version)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini %s endpoint failed (%s); trying next version.", version, exc)
    # Both failed -- return v1beta anyway and let the caller surface the error naturally.
    logger.error("Both v1beta and v1 Gemini endpoints failed; returning v1beta client anyway.")
    return make_gemini_client(api_key, api_version="v1beta")


# ---------------------------------------------------------------------------
# Model ID helpers
# ---------------------------------------------------------------------------

def _strip_model_id(name: str | None) -> str:
    """Return the bare slug (e.g. ``gemini-1.5-flash-latest``) for deduplication."""
    if not name:
        return ""
    raw = str(name).strip().removeprefix("models/")
    if "/" in raw:
        raw = raw.rsplit("/", maxsplit=1)[-1]
    return raw.strip()


def _full_model_id(slug_or_full: str) -> str:
    """Return ``models/<slug>`` -- the fully-qualified REST path segment."""
    slug = _strip_model_id(slug_or_full)
    return f"models/{slug}" if slug else slug


# ---------------------------------------------------------------------------
# Model introspection helpers
# ---------------------------------------------------------------------------

def _supports_generate_content(model: Any) -> bool:
    actions = getattr(model, "supported_actions", None)
    if not actions:
        return True  # assume capable if field is absent
    for a in actions:
        if str(a).lower().replace("-", "").replace("_", "") == "generatecontent":
            return True
    return False


def _list_models(client: genai.Client) -> list[Any]:
    rows: list[Any] = []
    try:
        for m in client.models.list():
            rows.append(m)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Gemini models.list() failed (%s).", exc)
    return rows


def _parse_version_score(model_id: str) -> float:
    low = model_id.lower()
    m = re.search(r"gemini-(\d+)\.(\d+)", low)
    if m:
        return float(m.group(1)) + int(m.group(2)) / 10.0
    m = re.search(r"gemini[_-]?(\d+)[_-](\d+)", low)
    if m:
        try:
            return float(m.group(1)) + int(m.group(2)) / 10.0
        except ValueError:
            pass
    m = re.search(r"imagen[_-]?(\d+)\.?(\d+)?", low)
    if m:
        minor = int(m.group(2) or "0") if m.lastindex and m.group(2) else 0
        return float(m.group(1)) + minor / 10.0
    return 0.0


def _is_text_candidate(model_id: str) -> bool:
    low = model_id.lower()
    if not low.startswith("gemini"):
        return False
    if "embed" in low or "embedding" in low:
        return False
    if "imagen" in low:
        return False
    if re.search(r"gemini\b.*-image|gemini\b.*image-preview|imagegeneration", low):
        return False
    return True


def _is_image_candidate(model_id: str) -> bool:
    low = model_id.lower()
    return "imagen" in low or ("gemini" in low and "image" in low)


def _text_sort_key(model_id: str) -> tuple[float, float, str]:
    low = model_id.lower()
    tier = 1.5
    if "flash-lite" in low:
        tier = 0.0
    elif "flash" in low:
        tier = 2.0
    elif "pro" in low:
        tier = 3.0
    # Prefer -latest aliases (they always resolve to something real).
    bonus = 0.05 if "latest" in low else 0.0
    return (tier + bonus, _parse_version_score(model_id), low)


def _image_sort_key(model_id: str) -> tuple[float, float, str]:
    low = model_id.lower()
    pref = 1.0
    if re.search(r"imagen-[34]|imagen[34]", low):
        pref = 4.0
    elif "imagen" in low:
        pref = 3.5
    elif "gemini-3" in low and "image" in low:
        pref = 3.0
    elif "gemini-2" in low and "image" in low:
        pref = 2.5
    return (pref, _parse_version_score(model_id), low)


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

def _should_try_next_model(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code == 404:
        return True
    status = str(getattr(exc, "status", "") or "")
    if "404" in status or "NOT_FOUND" in status.upper():
        return True
    msg = str(getattr(exc, "message", None) or exc).lower()
    return "not found" in msg or "not_found" in msg or "no longer available" in msg or "404" in msg


def _is_retryable_503(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    if code == 503:
        return True
    status = str(getattr(exc, "status", "") or "").upper()
    if "503" in status or "UNAVAILABLE" in status:
        return True
    msg = str(exc).lower()
    return "503" in msg or "unavailable" in msg or "high demand" in msg or "overloaded" in msg


def is_model_not_found_error(exc: BaseException) -> bool:
    return _should_try_next_model(exc)


# ---------------------------------------------------------------------------
# Handshake: get_active_model_id
# ---------------------------------------------------------------------------

def get_active_model_id(
    client: genai.Client,
    *,
    preference: str | list[str] = "2.5-flash",
    capability_type: Capability = "text",
) -> str:
    """
    Query models.list() live and return the best model matching ``preference``.

    Steps
    -----
    1. Call models.list() -- real network handshake.
    2. Filter for generateContent capability and the correct type (text/image).
    3. Among candidates, pick the first whose ID contains ``preference``.
    4. If no preference match, pick the highest-scored candidate (with a warning).
    5. If models.list() returns nothing at all, raise RuntimeError and log
       what IS available so the operator can diagnose the issue.
    """
    raw_list = _list_models(client)

    candidates: list[str] = []
    for m in raw_list:
        if not _supports_generate_content(m):
            continue
        mid = _strip_model_id(getattr(m, "name", None))
        if not mid:
            continue
        if capability_type == "text" and not _is_text_candidate(mid):
            continue
        if capability_type == "image" and not _is_image_candidate(mid):
            continue
        candidates.append(mid)

    if not candidates:
        all_names = [str(getattr(m, "name", "?")) for m in raw_list]
        logger.error(
            "GEMINI HANDSHAKE FAILED: no generateContent-capable %s models found.\n"
            "All models returned by API:\n%s",
            capability_type,
            "\n".join(all_names) if all_names else "  (empty list -- bad API key or network?)",
        )
        raise RuntimeError(
            f"Gemini models.list() returned no {capability_type} models supporting generateContent. "
            f"Available: {all_names}"
        )

    prefs = [preference] if isinstance(preference, str) else list(preference)
    for pref in prefs:
        pref_lower = pref.lower()
        for mid in candidates:
            if pref_lower in mid.lower():
                result = _full_model_id(mid)
                logger.info(
                    "GEMINI HANDSHAKE OK: selected '%s' (preference='%s').", result, pref
                )
                return result

    # No preference matched -- fall back to best scored candidate.
    sort_key = _text_sort_key if capability_type == "text" else _image_sort_key
    best = sorted(candidates, key=sort_key, reverse=True)[0]
    result = _full_model_id(best)
    logger.warning(
        "GEMINI HANDSHAKE: none of %s found in live list; "
        "using best available '%s'. Live candidates: %s",
        prefs,
        result,
        [_full_model_id(c) for c in candidates],
    )
    return result


# ---------------------------------------------------------------------------
# Validated model chain builder
# ---------------------------------------------------------------------------

def build_model_chain(
    client: genai.Client,
    *,
    capability_type: Capability,
    preferred: str | None,
) -> list[str]:
    """
    Build a prioritised model chain containing ONLY models confirmed by models.list().

    If models.list() returns nothing (network failure at init), falls back to
    the _LAST_RESORT_* lists with a loud warning -- these are NOT validated.
    """
    preferred_slug = _strip_model_id(preferred) if preferred else ""
    chain: list[str] = []
    seen: set[str] = set()

    def add(slug: str) -> None:
        s = _strip_model_id(slug)
        if not s or s in seen:
            return
        seen.add(s)
        chain.append(_full_model_id(s))

    raw_list = _list_models(client)
    live_slugs: set[str] = set()
    scored: list[tuple[tuple[float, float, str], str]] = []

    for m in raw_list:
        if not _supports_generate_content(m):
            continue
        mid = _strip_model_id(getattr(m, "name", None))
        if not mid:
            continue
        live_slugs.add(mid)
        if capability_type == "text" and not _is_text_candidate(mid):
            continue
        if capability_type == "image" and not _is_image_candidate(mid):
            continue
        key = _text_sort_key(mid) if capability_type == "text" else _image_sort_key(mid)
        scored.append((key, mid))

    scored.sort(key=lambda kv: kv[0], reverse=True)

    # Preferred model goes first -- only if it is confirmed live.
    if preferred_slug:
        if preferred_slug in live_slugs:
            add(preferred_slug)
        else:
            logger.warning(
                "Preferred model '%s' is NOT in the live models.list(); skipping. "
                "Live models: %s",
                _full_model_id(preferred_slug),
                [_full_model_id(s) for s in sorted(live_slugs)],
            )

    # All live candidates, best first.
    for _, mid in scored:
        add(mid)

    if chain:
        return chain

    # models.list() returned nothing -- last resort.
    last_resort = _LAST_RESORT_TEXT if capability_type == "text" else _LAST_RESORT_IMAGE
    logger.warning(
        "models.list() returned no usable models (network issue?). "
        "Falling back to hardcoded list (NOT validated against live API): %s",
        last_resort,
    )
    for mid in last_resort:
        add(mid)
    return chain


# ---------------------------------------------------------------------------
# Resilient content generation: 503 backoff + 404 rotation + chain abort
# ---------------------------------------------------------------------------

def generate_content_with_model_fallback(
    client: genai.Client,
    model_chain: list[str],
    *,
    contents: Any,
    config: Any | None = None,
) -> Any:
    """
    Generate content down the chain with two resilience layers:

    * 503 / UNAVAILABLE -> exponential backoff (2^n s) up to MAX_503_RETRIES.
    * 404 / NOT_FOUND   -> log available models and advance chain immediately.

    If the chain is exhausted, re-raises the last exception.
    """
    if not model_chain:
        raise RuntimeError("generate_content_with_model_fallback: model_chain is empty.")

    last: BaseException | None = None
    total = len(model_chain)

    for i, model_id in enumerate(model_chain):
        next_id = model_chain[i + 1] if i + 1 < total else None

        for attempt in range(MAX_503_RETRIES):
            try:
                if config is not None:
                    response = client.models.generate_content(
                        model=model_id, contents=contents, config=config
                    )
                else:
                    response = client.models.generate_content(
                        model=model_id, contents=contents
                    )
                logger.info("Gemini OK | model=%s | attempt=%d", model_id, attempt)
                return response

            except (genai_errors.APIError, Exception) as exc:  # noqa: BLE001
                last = exc

                if _is_retryable_503(exc):
                    if attempt < MAX_503_RETRIES - 1:
                        wait = 2 ** (attempt + 1)
                        logger.warning(
                            "Gemini 503 on '%s' (attempt %d/%d); backing off %ds.",
                            model_id, attempt + 1, MAX_503_RETRIES, wait,
                        )
                        time.sleep(wait)
                        continue
                    logger.warning(
                        "Gemini 503 on '%s' -- all %d retry attempts exhausted. Advancing chain.",
                        model_id, MAX_503_RETRIES,
                    )
                    break

                if _should_try_next_model(exc):
                    if next_id:
                        logger.warning(
                            "Gemini 404/NOT_FOUND on '%s'; advancing to '%s'.",
                            model_id, next_id,
                        )
                    else:
                        logger.error(
                            "Gemini 404/NOT_FOUND on '%s' -- no remaining models in chain.",
                            model_id,
                        )
                    break

                raise  # non-retryable, non-404 error

    if last:
        raise last
    raise RuntimeError("generate_content_with_model_fallback: chain exhausted with no response.")


def _log_fallback_chain_step(failed_model: str, exc: BaseException, next_model: str | None) -> None:
    if next_model:
        logger.warning("GEMINI_ALERT: '%s' failed (%s); trying '%s'.", failed_model, exc, next_model)
    else:
        logger.warning("GEMINI_ALERT: '%s' failed (%s); no remaining models.", failed_model, exc)


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

def get_best_available_model(
    client: genai.Client,
    *,
    capability_type: Capability = "text",
) -> str | None:
    """Pick the single best model ID (fully qualified) for this key."""
    models_list = _list_models(client)
    scored: list[tuple[tuple[float, float, str], str]] = []
    for m in models_list:
        if not _supports_generate_content(m):
            continue
        mid = _strip_model_id(getattr(m, "name", None))
        if not mid:
            continue
        if capability_type == "text" and not _is_text_candidate(mid):
            continue
        if capability_type == "image" and not _is_image_candidate(mid):
            continue
        key = _text_sort_key(mid) if capability_type == "text" else _image_sort_key(mid)
        scored.append((key, mid))
    if not scored:
        return None
    scored.sort(key=lambda kv: kv[0], reverse=True)
    return _full_model_id(scored[0][1])


def get_latest_model(client: genai.Client, *, kind: Capability = "text") -> str:
    try:
        found = get_best_available_model(client, capability_type=kind)
        if found:
            return found
    except Exception as exc:  # noqa: BLE001
        logger.warning("Gemini model discovery raised %s; using safe default.", exc)
    import config as app_cfg
    fb = app_cfg.SAFE_GEMINI_IMAGE_MODEL if kind == "image" else app_cfg.SAFE_GEMINI_TEXT_MODEL
    logger.warning("GEMINI_ALERT: discovery failed (kind=%s); using '%s'.", kind, fb)
    return fb


def generate_text_with_client_chain(
    *,
    api_key: str,
    preferred_model: str | None,
    contents: Any,
) -> Any:
    client = make_gemini_client_with_fallback(api_key)
    chain = build_model_chain(client, capability_type="text", preferred=preferred_model)
    return generate_content_with_model_fallback(client, chain, contents=contents)


def normalize_model_id(model_id: str | None) -> str:
    return _strip_model_id(model_id)


def chain_with_preferred_first(base_chain: list[str], preferred: str | None) -> list[str]:
    """Prepend preferred model (full models/<slug> form) without duplicating."""
    pref_slug = _strip_model_id(preferred)
    if not pref_slug:
        return list(base_chain)
    pref_full = _full_model_id(pref_slug)
    rest = [x for x in base_chain if _strip_model_id(x) != pref_slug]
    return [pref_full] + rest
