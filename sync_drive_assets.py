# -*- coding: utf-8 -*-
"""
sync_drive_assets.py
--------------------
Pinterest Sales & Recycling Engine -- Phase 1: Database Expansion & Library Repair.

For every JSON in outputs/library/ this script:
  1. Resolves local_image_path from the image_relative field (G: Drive path).
  2. Generates pinterest_title  -- 100-char SEO title.
  3. Generates pinterest_caption -- sales-optimised caption (social CTAs removed,
     direct Payhip link injected).
  4. Generates visual_hook      -- short ALL-CAPS overlay text for the 2:3 pin.

By default uses Claude (Anthropic) for richer AI generation.
Use --no-ai to fall back to fast regex + template generation.

Usage:
    python sync_drive_assets.py               # process all unprocessed posts
    python sync_drive_assets.py --limit 20    # process at most 20 posts
    python sync_drive_assets.py --force       # re-generate even if fields already exist
    python sync_drive_assets.py --no-ai       # skip Claude, use fast regex/templates
    python sync_drive_assets.py --dry-run     # preview only, no writes
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: ensure project root is on sys.path and .env is loaded
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402  -- loads .env

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sync_drive_assets")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SALES_URL = "http://blueprint.holisticprotocolslab.com/"

SALES_CTA_VARIANTS = [
    f"The complete Holistic Legacy Protocol is available for download   click the link at {SALES_URL}",
    f"Access the full scientific guide at {SALES_URL}   every protocol, every mechanism, in one place.",
    f"Download the complete Holistic Legacy Protocol at {SALES_URL}",
    f"If you want the full protocol with every step and mechanism, it is available at {SALES_URL}",
]

# Patterns that identify Instagram/Facebook social CTAs to be removed
_SOCIAL_CTA_RE = re.compile(
    r"(?:"
    # "comment KEYWORD below and I will send it to you."
    r"[Cc]omment\s+[A-Z]+(?:\s+below)?(?:\s+and\s+I[^.!?]*)?[.!?]?"
    r"|"
    # "Type KEYWORD in the comments" variants
    r"[Tt]ype\s+[A-Z]+\s+(?:below|in\s+the\s+comments)[^.!?]*[.!?]?"
    r"|"
    # "DM me" / "send me a DM"
    r"(?:[Ss]end\s+(?:me\s+)?a\s+(?:DM|message)|DM\s+me)[^.!?]*[.!?]?"
    r"|"
    # "drop a comment below"
    r"[Dd]rop\s+a\s+comment[^.!?]*[.!?]?"
    r")",
    re.MULTILINE,
)

# Pillar hashtags for Pinterest
PINTEREST_HASHTAGS = (
    "#NaturalHealth #HolisticProtocol #CellularHealing "
    "#NaturalRemedies #HolisticLiving"
)

# Banned hype words from DNA
_BANNED_WORDS = ["unlock", "discover", "elevate", "game-changer", "harness",
                 "revolutionary", "dive"]


# ---------------------------------------------------------------------------
# Regex / template fallback helpers
# ---------------------------------------------------------------------------

def _clean_social_ctas(text: str) -> str:
    """Remove all detected social engagement CTAs from the caption."""
    cleaned = _SOCIAL_CTA_RE.sub("", text)
    # Collapse triple+ newlines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _inject_sales_cta(text: str, variant_index: int = 0) -> str:
    """Append a rotating sales CTA paragraph."""
    cta = SALES_CTA_VARIANTS[variant_index % len(SALES_CTA_VARIANTS)]
    return f"{text.rstrip()}\n\n{cta}"


def _build_pinterest_title_template(topic: str) -> str:
    """
    Generate a keyword-rich SEO title from the topic string.
    Guaranteed <= 100 characters.
    """
    # Capitalise every word
    base = topic.strip().title()

    suffixes = [
        " | Natural Healing Protocol",
        " | Holistic Science Guide",
        " | Anna's Natural Protocol",
        "   Real Food Science",
        " Protocol | Cellular Healing",
    ]
    for sfx in suffixes:
        candidate = base + sfx
        if len(candidate) <= 100:
            return candidate
    return base[:100]


def _build_visual_hook_template(topic: str) -> str:
    """Short punchy ALL-CAPS overlay text derived from the topic."""
    words = topic.upper().split()
    # Keep 4-6 words maximum
    hook_words = words[:5]
    return " ".join(hook_words)


def _transform_caption_regex(
    caption: str, topic: str, variant_index: int = 0
) -> str:
    """
    Fast regex-only caption transformation.
    Returns empty string if caption is a placeholder.
    """
    if not caption or caption.strip() in ("PENDING_CAPTION", "pending", ""):
        return ""
    cleaned = _clean_social_ctas(caption)
    result = _inject_sales_cta(cleaned, variant_index)
    # Remove any banned hype words (case-insensitive)
    for word in _BANNED_WORDS:
        result = re.sub(rf"\b{re.escape(word)}\b", "", result, flags=re.IGNORECASE)
    result = re.sub(r"  +", " ", result)
    return result.strip()


# ---------------------------------------------------------------------------
# AI-assisted generation (Claude)
# ---------------------------------------------------------------------------

def _load_anthropic_client():
    """Return an Anthropic client or None if not available."""
    try:
        import anthropic  # noqa: PLC0415
        api_key = config.ANTHROPIC_API_KEY
        if not api_key:
            log.warning("ANTHROPIC_API_KEY not set; falling back to regex mode.")
            return None, None
        client = anthropic.Anthropic(api_key=api_key)
        from config import get_best_claude_model  # noqa: PLC0415
        model = get_best_claude_model(client)
        log.info("Claude model resolved: %s", model)
        return client, model
    except ImportError:
        log.warning("anthropic package not installed; falling back to regex mode.")
        return None, None


_AI_SYSTEM_PROMPT = """\
You are Anna, a 72-year-old holistic health authority: silver-haired, \
grandmotherly, scientifically precise, humble. You speak in plain warm language \
with biochemical authority. You NEVER use hype words: unlock, discover, elevate, \
game-changer, harness, revolutionary, dive.
"""

_AI_USER_TEMPLATE = """\
TASK: Transform this Instagram/Facebook post into a Pinterest sales post.

TOPIC: {topic}

SOURCE CAPTION:
{caption}

SOURCE CONTENT (if caption is empty, use this):
{fact_sheet_excerpt}

RULES:
1. REMOVE every "Comment [KEYWORD]", "Type [KEYWORD]", "DM me", or \
"Send a message" CTA completely.
2. REPLACE with a natural-sounding, non-pushy closing line pointing readers to: \
{sales_url}
   Example endings: "The full protocol   every step, every mechanism   is waiting \
at {sales_url}" or "If this helped, the complete guide is available for \
download at {sales_url}"
3. Maintain Anna's voice: humble, science-based, grandmotherly, specific.
4. The pinterest_title must be a keyword-rich SEO phrase, exactly 100 characters \
or fewer. Include words like "Protocol", "Natural", "Cellular", or the topic keyword.
5. The visual_hook is 5 8 words in ALL CAPS for overlay on the pin image.

OUTPUT: Respond with ONLY valid JSON (no markdown fences, no explanation):
{{
  "pinterest_title": "<max 100 chars>",
  "pinterest_caption": "<full transformed caption>",
  "visual_hook": "<5-8 WORD ALL-CAPS HOOK>"
}}
"""


def _generate_metadata_ai(
    topic: str,
    source_caption: str,
    raw_fact_sheet: str,
    client,
    model: str,
    variant_index: int = 0,
) -> dict[str, str]:
    """
    Call Claude to generate pinterest_title, pinterest_caption, visual_hook.
    Returns a dict with those three keys. On failure returns empty dict.
    """
    fact_excerpt = (raw_fact_sheet or "")[:600]

    user_msg = _AI_USER_TEMPLATE.format(
        topic=topic,
        caption=source_caption if source_caption not in ("PENDING_CAPTION", "") else "(none)",
        fact_sheet_excerpt=fact_excerpt,
        sales_url=SALES_URL,
    )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=900,
            system=_AI_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```[a-z]*\n?", "", raw).rstrip("```").strip()
        data = json.loads(raw)
        return {
            "pinterest_title": str(data.get("pinterest_title", ""))[:100],
            "pinterest_caption": str(data.get("pinterest_caption", "")),
            "visual_hook": str(data.get("visual_hook", "")),
        }
    except json.JSONDecodeError as exc:
        log.warning("JSON parse failed for topic '%s': %s", topic, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("Claude call failed for topic '%s': %s", topic, exc)
    return {}


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_local_image(record: dict) -> str | None:
    """
    Resolve the physical image path from the JSON record.
    Tries:
      1. existing local_image_path field
      2. image_relative field resolved against ENGINE_ROOT
    Returns the absolute path string if the file exists, else None.
    """
    # 1. Already set and valid
    existing = record.get("local_image_path", "")
    if existing and Path(existing).is_file():
        return str(Path(existing).resolve())

    # 2. Resolve from image_relative
    rel = record.get("image_relative", "")
    if not rel:
        return None
    candidate = (config.ENGINE_ROOT / rel).resolve()
    if candidate.is_file():
        return str(candidate)

    # 3. Try absolute G: path construction (for Drive paths)
    # image_relative is like "outputs/assets/topic_slug/file.png"
    drive_candidate = Path(r"G:\My Drive\Z sosFiles\Z_act\@ NETWORK\@_Content 2026"
                           r"\The Holistic Legacy - Anna's Protocol"
                           r"\Anna's Automated Image Posts Engine") / rel
    if drive_candidate.is_file():
        return str(drive_candidate.resolve())

    return None


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def _needs_processing(record: dict, force: bool) -> bool:
    """Return True if the record is missing pinterest fields (or force=True)."""
    if force:
        return True
    return not (
        record.get("pinterest_title")
        and record.get("pinterest_caption")
        and record.get("visual_hook")
    )


def sync_library(
    limit: int | None = None,
    force: bool = False,
    dry_run: bool = False,
    use_ai: bool = True,
    ai_delay_sec: float = 0.8,
) -> dict[str, int]:
    """
    Main sync function. Returns a stats dict.
    """
    library_dir = config.LIBRARY_DIR
    json_files = sorted(library_dir.glob("post_*.json"))

    if not json_files:
        log.warning("No post JSON files found in %s", library_dir)
        return {"total": 0, "processed": 0, "skipped": 0, "errors": 0}

    log.info("Found %d library records in %s", len(json_files), library_dir)

    # Load Claude if AI mode requested
    client, model = (None, None)
    if use_ai:
        client, model = _load_anthropic_client()
        if client is None:
            log.info("AI unavailable   using regex/template mode.")
            use_ai = False

    stats = {"total": len(json_files), "processed": 0, "skipped": 0, "errors": 0}
    processed_count = 0

    for json_path in json_files:
        if limit is not None and processed_count >= limit:
            log.info("Limit of %d reached. Stopping.", limit)
            break

        try:
            record = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to read %s: %s", json_path.name, exc)
            stats["errors"] += 1
            continue

        if not _needs_processing(record, force):
            stats["skipped"] += 1
            continue

        topic = record.get("topic", "Holistic Health Protocol")
        source_caption = record.get("humanized_caption", "")
        raw_fact_sheet = record.get("raw_fact_sheet", "")
        variant_index = record.get("variant_index", 0)

        log.info("Processing: %s (variant %s)", topic, variant_index)

        # --- Resolve image path ---
        local_path = _resolve_local_image(record)
        if local_path:
            record["local_image_path"] = local_path
            log.debug("  Image resolved: %s", local_path)
        else:
            # Store imgbb URL as fallback reference
            record["local_image_path"] = record.get("imgbb_url", "")
            log.debug("  Image not found locally; imgbb_url stored as fallback.")

        # --- Generate Pinterest metadata ---
        if use_ai and client:
            metadata = _generate_metadata_ai(
                topic, source_caption, raw_fact_sheet, client, model, variant_index
            )
            if metadata:
                record["pinterest_title"] = metadata["pinterest_title"]
                record["pinterest_caption"] = metadata["pinterest_caption"]
                record["visual_hook"] = metadata["visual_hook"]
                log.info("  AI metadata injected for: %s", topic)
                time.sleep(ai_delay_sec)  # gentle rate limiting
            else:
                log.warning("  AI failed; falling back to regex for: %s", topic)
                use_ai_fallback = True
        else:
            use_ai_fallback = True

        if not use_ai or not record.get("pinterest_title"):
            record.setdefault(
                "pinterest_title",
                _build_pinterest_title_template(topic),
            )
            record.setdefault(
                "pinterest_caption",
                _transform_caption_regex(source_caption, topic, variant_index) or (
                    f"Natural health protocol for {topic.lower()}. "
                    f"The complete Holistic Legacy guide is at {SALES_URL}"
                ),
            )
            record.setdefault(
                "visual_hook",
                _build_visual_hook_template(topic),
            )

        # Ensure destination URL is always embedded
        record["pinterest_destination_url"] = SALES_URL

        # --- Write back ---
        if not dry_run:
            try:
                json_path.write_text(
                    json.dumps(record, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to write %s: %s", json_path.name, exc)
                stats["errors"] += 1
                continue
        else:
            log.info("  [DRY RUN] Would write: %s", json_path.name)
            log.info("  pinterest_title:   %s", record.get("pinterest_title"))
            log.info("  visual_hook:       %s", record.get("visual_hook"))
            log.info("  caption_preview:   %s...", str(record.get("pinterest_caption", ""))[:120])

        stats["processed"] += 1
        processed_count += 1

    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Drive assets and inject Pinterest metadata into the library."
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Maximum number of posts to process (default: all).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-generate metadata even if pinterest fields already exist.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview without writing any changes.",
    )
    parser.add_argument(
        "--no-ai", action="store_true",
        help="Skip Claude; use fast regex/template mode instead.",
    )
    parser.add_argument(
        "--ai-delay", type=float, default=0.8,
        help="Seconds to wait between Claude API calls (default: 0.8).",
    )
    args = parser.parse_args()

    stats = sync_library(
        limit=args.limit,
        force=args.force,
        dry_run=args.dry_run,
        use_ai=not args.no_ai,
        ai_delay_sec=args.ai_delay,
    )

    print(
        f"\n'  Sync complete   "
        f"total={stats['total']}  processed={stats['processed']}  "
        f"skipped={stats['skipped']}  errors={stats['errors']}"
    )


if __name__ == "__main__":
    _cli()
