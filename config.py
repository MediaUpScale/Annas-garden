# -*- coding: utf-8 -*-
"""
Central paths and credentials for Anna's Automated Image Posts Engine.

Environment variables load from `.env` in the project root.

Supported keys:
    GEMINI_API_KEY (or GOOGLE_API_KEY), ANTHROPIC_API_KEY,
    GEMINI_IMAGE_MODEL, GEMINI_IMAGE_ASPECT_RATIO,
    GEMINI_RESEARCH_MODEL, CLAUDE_MODEL,
    GEMINI_ECONOMIC_BRAIN_MODEL, ECONOMIC_BRAIN_MODE (true/false),
    REFERENCE_IMAGE_PATH, DIGITAL_PRODUCTS_PATH, OUTPUTS_DIR, PDF_CHUNK_CHAR_LIMIT,
    IMGBB_API_KEY, ANTHROPIC_API_VERSION,
    PUBLISHING_SCHEDULE (e.g. "3h" or "90m"   spacing between variant posts)
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

ENGINE_ROOT: Path = Path(__file__).resolve().parent
DOTENV_PATH: Path = ENGINE_ROOT / ".env"


def _load_project_dotenv() -> tuple[Path, bool]:
    resolved = DOTENV_PATH.expanduser().resolve()
    if not resolved.is_file():
        return resolved, False

    # Use utf-8-sig so any UTF-8 BOM injected by Google Drive sync is
    # silently stripped before parsing — prevents \ufeffGEMINI_API_KEY ghost keys.
    raw = resolved.read_bytes()
    UTF8_BOM = bytes([0xEF, 0xBB, 0xBF])
    if raw[:3] == UTF8_BOM:
        resolved.write_bytes(raw[3:])
        logger.debug(".env BOM stripped automatically.")

    return resolved, bool(load_dotenv(dotenv_path=resolved, override=True, encoding="utf-8-sig"))


_DOTENV_RESOLVED_PATH, DOTENV_LOADED_FROM_FILE = _load_project_dotenv()


def print_dotenv_bootstrap() -> None:
    if DOTENV_LOADED_FROM_FILE:
        print(f"[bootstrap] .env loaded: {_DOTENV_RESOLVED_PATH}")
    else:
        print(f"[bootstrap] .env not loaded from {_DOTENV_RESOLVED_PATH}")


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _resolve_path(value: str | None, default: Path) -> Path:
    return Path((value or str(default))).expanduser()


def _parse_schedule_minutes(raw: str | None) -> int | None:
    """Parse '3h', '90m', '120' (bare minutes) �! integer minutes; None if unset."""
    if not raw:
        return None
    raw = raw.strip().lower()
    m = re.fullmatch(r"(\d+)\s*h(?:ours?)?", raw)
    if m:
        return int(m.group(1)) * 60
    m = re.fullmatch(r"(\d+)\s*m(?:in(?:utes?)?)?", raw)
    if m:
        return int(m.group(1))
    if raw.isdigit():
        return int(raw)
    return None


# ---------------------------------------------------------------------------
# Safe fallback model IDs (used when API discovery returns nothing)
# ---------------------------------------------------------------------------
SAFE_GEMINI_TEXT_MODEL: str = "models/gemini-2.5-flash"
SAFE_GEMINI_IMAGE_MODEL: str = "models/gemini-3-pro-image-preview"
# Fallback when Models API discovery yields nothing.
SAFE_CLAUDE_MODEL: str = "claude-3-5-sonnet-latest"

# ---------------------------------------------------------------------------
# API keys & versioning
# ---------------------------------------------------------------------------
GEMINI_API_KEY: str | None = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_API_VERSION: str = (os.getenv("ANTHROPIC_API_VERSION") or "2023-06-01").strip()
IMGBB_API_KEY: str | None = os.getenv("IMGBB_API_KEY")

# ---------------------------------------------------------------------------
# Model IDs  (env overrides; otherwise discovered at runtime)
# ---------------------------------------------------------------------------
# Gemini text: prefer env override, fall back to flash (dynamic discovery at CaptionEngine init).
GEMINI_RESEARCH_MODEL: str = os.getenv("GEMINI_RESEARCH_MODEL", SAFE_GEMINI_TEXT_MODEL)
GEMINI_ECONOMIC_BRAIN_MODEL: str = os.getenv("GEMINI_ECONOMIC_BRAIN_MODEL", SAFE_GEMINI_TEXT_MODEL)
# Claude: -latest alias avoids 404s on stale date-versioned strings.
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", SAFE_CLAUDE_MODEL)

GEMINI_IMAGE_MODEL: str = os.getenv("GEMINI_IMAGE_MODEL", SAFE_GEMINI_IMAGE_MODEL)
GEMINI_IMAGE_ASPECT_RATIO: str = os.getenv("GEMINI_IMAGE_ASPECT_RATIO", "3:4")
ECONOMIC_BRAIN_MODE: bool = _bool_env("ECONOMIC_BRAIN_MODE", False)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_REFERENCE_AVATAR_DEFAULT = Path(
    r"G:\My Drive\Z sosFiles\Z_act\@ NETWORK\@_Content 2026\The Holistic Legacy - Anna's Protocol"
    r"\Anna's Automated Image Posts Engine\avatar_reference\avatar.png",
)

_DEFAULT_DIGITAL_PRODUCTS = ENGINE_ROOT / "product_reference" / "Digital Products"
_DEFAULT_OUTPUTS = ENGINE_ROOT / "outputs"

DIGITAL_PRODUCTS_PATH: Path = _resolve_path(os.getenv("DIGITAL_PRODUCTS_PATH"), _DEFAULT_DIGITAL_PRODUCTS)
OUTPUTS_DIR: Path = _resolve_path(os.getenv("OUTPUTS_DIR"), _DEFAULT_OUTPUTS)
REFERENCE_IMAGE_PATH: Path = _resolve_path(os.getenv("REFERENCE_IMAGE_PATH"), _REFERENCE_AVATAR_DEFAULT)

PDF_CHUNK_CHAR_LIMIT: int = int(os.getenv("PDF_CHUNK_CHAR_LIMIT", "48000"))
PERSONA_DNA_PATH: Path = ENGINE_ROOT / "persona_dna.py"
MASTER_DNA_PATH: Path = ENGINE_ROOT / "avatar_engine" / "master_dna.json"

# Image model handshake preference (verified against models.list() at runtime)
GEMINI_IMAGE_MODEL_PREFERENCE: str = "models/gemini-3-pro-image-preview"

ASSETS_DIR: Path = OUTPUTS_DIR / "assets"
LIBRARY_DIR: Path = OUTPUTS_DIR / "library"
CONTENT_LIBRARY_PATH: Path = OUTPUTS_DIR / "content_library.json"

_SAMPLE_BULK_V3: Path = ENGINE_ROOT / "sample_bulk_posts_import_3.xlsx"
_SAMPLE_BULK_LEGACY: Path = ENGINE_ROOT / "sample_bulk_posts_import.xlsx"
BULK_POSTS_TEMPLATE_XLSX: Path = _SAMPLE_BULK_V3 if _SAMPLE_BULK_V3.is_file() else _SAMPLE_BULK_LEGACY
POST_PLANNER_XLSX: Path = OUTPUTS_DIR / "automated_bulk_posts_import.xlsx"

_LEGACY_PLANNER_XLSX: Path = ENGINE_ROOT / "automated_bulk_posts_import.xlsx"

# ---------------------------------------------------------------------------
# Publishing schedule (Instagram / PostPlanner)
# ---------------------------------------------------------------------------
PUBLISHING_SCHEDULE: str | None = os.getenv("PUBLISHING_SCHEDULE") or None
PUBLISHING_INTERVAL_MINUTES: int | None = _parse_schedule_minutes(PUBLISHING_SCHEDULE)

# ---------------------------------------------------------------------------
# Pinterest safe-drip interval (human-mimic scheduler)
# ---------------------------------------------------------------------------
def _parse_interval_hours(name: str, default: float) -> float:
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default

MIN_INTERVAL_HOURS: float = _parse_interval_hours("MIN_INTERVAL_HOURS", 3.0)
MAX_INTERVAL_HOURS: float = _parse_interval_hours("MAX_INTERVAL_HOURS", 6.0)
PINTEREST_PINS_PER_DAY: int = int(os.getenv("PINTEREST_PINS_PER_DAY", "4"))

# ---------------------------------------------------------------------------
# Directory bootstrap
# ---------------------------------------------------------------------------
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
ASSETS_DIR.mkdir(parents=True, exist_ok=True)
LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

if _LEGACY_PLANNER_XLSX.is_file() and not POST_PLANNER_XLSX.exists():
    import shutil
    try:
        shutil.copy2(_LEGACY_PLANNER_XLSX, POST_PLANNER_XLSX)
    except OSError:
        logger.debug("Legacy planner copy skipped.", exc_info=True)


# ---------------------------------------------------------------------------
# Dynamic model discovery helpers (called by CaptionEngine / adapters)
# ---------------------------------------------------------------------------

def get_best_claude_model(anthropic_client: object | None = None) -> str:
    """
    Query the Anthropic Models API (GET /v1/models) and return the best available
    conversational model. Priority: sonnet > haiku > any other.
    Falls back to SAFE_CLAUDE_MODEL if the API call fails or returns nothing.
    """
    if anthropic_client is None:
        return CLAUDE_MODEL or SAFE_CLAUDE_MODEL
    try:
        page = anthropic_client.models.list()  # type: ignore[union-attr]
        models = list(getattr(page, "data", None) or page)
        # Prefer sonnet, then haiku; skip embedding / moderation models.
        for priority in ("sonnet", "haiku"):
            for m in models:
                mid = str(getattr(m, "id", "") or "").lower()
                if priority in mid and "claude" in mid:
                    logger.debug("Dynamic Claude model resolved: %s", mid)
                    return mid
        # Last resort: first Claude model in the list
        for m in models:
            mid = str(getattr(m, "id", "") or "")
            if "claude" in mid.lower():
                return mid
    except Exception as exc:  # noqa: BLE001
        logger.debug("Claude model discovery failed (%s); using configured fallback.", exc)
    return CLAUDE_MODEL or SAFE_CLAUDE_MODEL


def get_best_gemini_text_model(client: object | None = None) -> str:  # type: ignore[type-arg]
    """
    Query Gemini models.list() and return the highest-scoring GA 'flash' or 'pro' text model.
    Falls back to SAFE_GEMINI_TEXT_MODEL if discovery fails or yields nothing.
    """
    if client is None:
        return GEMINI_RESEARCH_MODEL or SAFE_GEMINI_TEXT_MODEL
    try:
        from avatar_engine.providers.gemini_utils import (  # avoid circular at module load
            _list_models,
            _parse_version_score,
            _strip_model_id,
            _supports_generate_content,
        )
        candidates = []
        for m in _list_models(client):
            mid = _strip_model_id(getattr(m, "name", None))
            if not mid:
                continue
            low = mid.lower()
            if not any(k in low for k in ("flash", "pro")):
                continue
            if "image" in low or "vision" in low or "embed" in low:
                continue
            if not _supports_generate_content(m):
                continue
            candidates.append((mid, _parse_version_score(mid)))
        if candidates:
            best = max(candidates, key=lambda x: x[1])[0]
            logger.debug("Dynamic Gemini text model resolved: %s", best)
            return best
    except Exception as exc:  # noqa: BLE001
        logger.debug("Gemini model discovery failed (%s); using fallback.", exc)
    return GEMINI_RESEARCH_MODEL or SAFE_GEMINI_TEXT_MODEL


# ---------------------------------------------------------------------------
# Avatar helpers
# ---------------------------------------------------------------------------

def reference_avatar_resolved_path() -> Path:
    return REFERENCE_IMAGE_PATH.resolve()


def reference_avatar_exists() -> bool:
    return REFERENCE_IMAGE_PATH.is_file()


def warn_if_reference_avatar_missing() -> None:
    if reference_avatar_exists():
        return
    logger.warning(
        "Reference likeness file not found at %s. Image generation falls back to text-only prompting.",
        reference_avatar_resolved_path(),
    )
