# -*- coding: utf-8 -*-
"""
postplanner_exporter.py
-----------------------
Post Planner Bulk Export Engine for Anna's Holistic Legacy.

Reads from outputs/master_inventory.json and generates a CSV in the exact
format accepted by Post Planner's bulk upload tool:

    POSTING TIME,CAPTION,CONTENT: LINK,CONTENT: MEDIA,POST TYPE

Scheduling logic:
  - Starts tomorrow at the first configured slot
  - Posts DEFAULT_POSTS_PER_DAY times per day (configurable)
  - Slots default to 08:00, 13:00, 18:00 (local / US-ET aligned)
  - Each slot is offset by a configurable timezone nudge

Filters:
  - Only exports entries where posted_on_instagram == False (unscheduled)
  - Skips entries with no image (imgbb_url empty AND local_image_path missing)

Caption treatment:
  - Uses original_caption (the humanized Instagram caption)
  - Strips "Comment [KEYWORD]" / "DM me" CTAs
  - Appends a soft, non-pushy CTA pointing to the Payhip store

Usage:
    python postplanner_exporter.py                   # export all unscheduled
    python postplanner_exporter.py --limit 30        # export at most 30 entries
    python postplanner_exporter.py --posts-per-day 3 # 3 slots per day
    python postplanner_exporter.py --start 2026-07-01 # start from specific date
    python postplanner_exporter.py --mark-exported   # flip posted_on_instagram in inventory
    python postplanner_exporter.py --dry-run         # print preview, no file written
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env", override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(
            open(os.devnull, "w") if False   # placeholder
            else __import__("sys").stdout
        )
    ],
)
log = logging.getLogger("postplanner_exporter")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SALES_URL = "http://blueprint.holisticprotocolslab.com/"

# Post times (24h, local machine time or UTC   match your Post Planner timezone)
DEFAULT_SLOTS_24H = ["08:00", "13:00", "18:00"]
DEFAULT_POSTS_PER_DAY = 3

# Social CTA regex (same as inventory.py for consistency)
_SOCIAL_CTA_RE = re.compile(
    r"(?:"
    r"[Cc]omment\s+[A-Z]+(?:\s+below)?(?:\s+and\s+I[^.!?]*)?"
    r"|[Tt]ype\s+[A-Z]+\s+(?:below|in\s+the\s+comments)[^.!?]*"
    r"|(?:[Ss]end\s+(?:me\s+)?a\s+(?:DM|message)|DM\s+me)[^.!?]*"
    r"|[Dd]rop\s+a\s+comment[^.!?]*"
    r"|[Ss]earch\s+[A-Z]+\s+on\s+my\s+page[^.!?]*"
    r")[.!?]?",
    re.MULTILINE,
)

# Facebook-specific CTA options (soft, no "click link in pin" Pinterest language)
_FB_CTA_VARIANTS = [
    f"The full protocol is available at {SALES_URL}",
    f"Download the complete guide at {SALES_URL}",
    f"All protocols and mechanisms in one place: {SALES_URL}",
    f"The complete Holistic Legacy Protocol is at {SALES_URL}",
]

# CSV column headers (exact Post Planner format)
_CSV_HEADERS = ["POSTING TIME", "CAPTION", "CONTENT: LINK", "CONTENT: MEDIA", "POST TYPE"]

# Date/time format Post Planner expects
_POSTPLANNER_DT_FMT = "%m/%d/%Y %H:%M"


# ---------------------------------------------------------------------------
# Caption transformation
# ---------------------------------------------------------------------------

_RAW_DRAFT_RE = re.compile(
    r"^\s*(?:\[Researcher draft|\[Caption from researcher|"
    r"\*\*RAW FACT SHEET|\[humanizer unavailable)"
)


def _clean_for_facebook(caption: str, variant_idx: int = 0) -> str:
    """
    Strip social engagement CTAs and inject a Facebook-appropriate sales CTA.
    Returns empty string for raw/un-humanized researcher drafts.
    """
    if not caption or caption.strip() in ("PENDING_CAPTION", "pending", ""):
        return ""
    # Skip raw researcher drafts that were never humanized
    if _RAW_DRAFT_RE.search(caption):
        return ""

    cleaned = _SOCIAL_CTA_RE.sub("", caption)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    cta = _FB_CTA_VARIANTS[variant_idx % len(_FB_CTA_VARIANTS)]
    return f"{cleaned}\n\n{cta}"


# ---------------------------------------------------------------------------
# Schedule generation
# ---------------------------------------------------------------------------

def _build_schedule(
    n_entries: int,
    posts_per_day: int,
    slots: list[str],
    start_date: date | None = None,
) -> list[datetime]:
    """
    Generate n_entries posting datetimes spaced posts_per_day per day.
    Uses the provided slots (e.g. ['08:00', '13:00', '18:00']).
    """
    if not slots:
        slots = DEFAULT_SLOTS_24H

    # Use at most posts_per_day slots
    active_slots = slots[:posts_per_day]
    parsed = []
    for s in active_slots:
        h, m = int(s.split(":")[0]), int(s.split(":")[1])
        parsed.append((h, m))

    base = start_date or (date.today() + timedelta(days=1))
    schedule: list[datetime] = []
    day_cursor = base
    slot_idx = 0

    while len(schedule) < n_entries:
        h, m = parsed[slot_idx % len(parsed)]
        dt = datetime(day_cursor.year, day_cursor.month, day_cursor.day, h, m)
        schedule.append(dt)
        slot_idx += 1
        if slot_idx % len(parsed) == 0:
            day_cursor += timedelta(days=1)

    return schedule[:n_entries]


# ---------------------------------------------------------------------------
# Core export
# ---------------------------------------------------------------------------

def _pick_media(entry: dict) -> str:
    """Return the best available media URL or path for an entry."""
    # Prefer imgbb (public URL) for Post Planner's cloud scheduler
    imgbb = entry.get("imgbb_url", "")
    if imgbb and imgbb.startswith("http"):
        return imgbb
    # Fall back to local absolute path
    local = entry.get("local_image_path", "")
    if local and Path(local).is_file():
        return local
    return ""


def export_to_csv(
    limit: int | None = None,
    posts_per_day: int = DEFAULT_POSTS_PER_DAY,
    slots: list[str] | None = None,
    start_date: date | None = None,
    mark_exported: bool = False,
    dry_run: bool = False,
    output_dir: Path | None = None,
) -> Path | None:
    """
    Main export function. Returns the path to the generated CSV, or None on dry-run.
    """
    import config as cfg  # noqa: PLC0415

    inventory_path = cfg.OUTPUTS_DIR / "master_inventory.json"
    if not inventory_path.is_file():
        log.error("master_inventory.json not found. Run: python pinterest_main.py sync")
        return None

    data = json.loads(inventory_path.read_text(encoding="utf-8"))
    all_entries = data.get("entries", [])

    # Filter: not yet exported to Instagram/Facebook AND has media
    eligible = [
        e for e in all_entries
        if not e.get("publication_status", {}).get("posted_on_instagram")
        and _pick_media(e)
    ]

    if not eligible:
        log.warning("No unscheduled entries with media found in master_inventory.json.")
        return None

    if limit:
        eligible = eligible[:limit]

    log.info(
        "Exporting %d entries to Post Planner CSV (%d posts/day).",
        len(eligible), posts_per_day,
    )

    schedule = _build_schedule(
        len(eligible), posts_per_day, slots or DEFAULT_SLOTS_24H, start_date
    )

    rows: list[dict] = []
    for i, entry in enumerate(eligible):
        topic = entry.get("topic", "Holistic Protocol")
        caption_src = entry.get("original_caption") or entry.get("humanized_caption", "")
        caption = _clean_for_facebook(caption_src, i)

        if not caption:
            # Fallback: build from pinterest_caption if original is empty
            caption = _clean_for_facebook(
                entry.get("pinterest_caption", ""), i
            )
        if not caption:
            caption = f"Natural healing protocol: {topic}. {SALES_URL}"

        media = _pick_media(entry)
        posting_time = schedule[i].strftime(_POSTPLANNER_DT_FMT)

        rows.append({
            "POSTING TIME":   posting_time,
            "CAPTION":        caption,
            "CONTENT: LINK":  "",           # Post Planner reads link from caption
            "CONTENT: MEDIA": media,
            "POST TYPE":      "IMAGE",
            # Internal tracking (not written to CSV)
            "_post_id":       entry.get("post_id", ""),
        })

    if dry_run:
        log.info("[DRY RUN] Would export %d rows. Preview (first 3):", len(rows))
        for r in rows[:3]:
            log.info(
                "  %s | %s... | %s",
                r["POSTING TIME"],
                r["CAPTION"][:80].replace("\n", " "),
                r["CONTENT: MEDIA"][:60],
            )
        return None

    # Write CSV
    out_dir = output_dir or (cfg.OUTPUTS_DIR / "postplanner")
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = out_dir / f"postplanner_export_{timestamp}.csv"

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=_CSV_HEADERS,
            extrasaction="ignore",
            quoting=csv.QUOTE_ALL,
        )
        writer.writeheader()
        writer.writerows(rows)

    log.info("CSV written: %s  (%d rows)", csv_path, len(rows))

    # Optionally mark exported entries in master_inventory
    if mark_exported:
        updated = 0
        exported_ids = {r["_post_id"] for r in rows}
        for entry in all_entries:
            if entry.get("post_id") in exported_ids:
                status = entry.setdefault("publication_status", {})
                if not status.get("posted_on_instagram"):
                    status["posted_on_instagram"] = True
                    status["instagram_post_date"] = datetime.now(timezone.utc).isoformat()
                    updated += 1

        data["last_updated_utc"] = datetime.now(timezone.utc).isoformat()
        tmp = inventory_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(inventory_path)
        log.info(
            "Marked %d entries as posted_on_instagram=True in master_inventory.json.",
            updated,
        )

    _print_export_summary(csv_path, rows, posts_per_day)
    return csv_path


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------

def _print_export_summary(csv_path: Path, rows: list[dict], posts_per_day: int) -> None:
    first_dt = rows[0]["POSTING TIME"] if rows else "?"
    last_dt  = rows[-1]["POSTING TIME"] if rows else "?"
    days = max(1, len(rows) // posts_per_day)
    print(f"\nPost Planner Export Complete")
    print(f"  File        : {csv_path}")
    print(f"  Total posts : {len(rows)}")
    print(f"  Per day     : {posts_per_day}")
    print(f"  Spans       : {days} days  ({first_dt}  ->  {last_dt})")
    print(f"  Destination : {SALES_URL}")
    print(f"\n  Upload this file at:")
    print(f"  https://app.postplanner.com/  ->  Bulk Scheduling -> Upload CSV\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"Cannot parse date: {s}. Use YYYY-MM-DD.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export master_inventory to Post Planner bulk-upload CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Max entries to export (default: all unscheduled).")
    parser.add_argument("--posts-per-day", type=int, default=DEFAULT_POSTS_PER_DAY,
                        help=f"Posts per day (default: {DEFAULT_POSTS_PER_DAY}).")
    parser.add_argument("--slots", nargs="+", default=None,
                        metavar="HH:MM",
                        help="Posting times e.g. --slots 08:00 13:00 18:00")
    parser.add_argument("--start", type=_parse_date, default=None,
                        metavar="YYYY-MM-DD",
                        help="First posting date (default: tomorrow).")
    parser.add_argument("--mark-exported", action="store_true",
                        help="Set posted_on_instagram=True in master_inventory after export.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing the CSV.")

    args = parser.parse_args()

    export_to_csv(
        limit=args.limit,
        posts_per_day=args.posts_per_day,
        slots=args.slots,
        start_date=args.start,
        mark_exported=args.mark_exported,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
