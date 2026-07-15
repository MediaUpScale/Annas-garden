# -*- coding: utf-8 -*-
"""
backfill_history.py
-------------------
One-time utility: manually register already-published entries in
outputs/scheduled_history.txt without going through the Pinterest API.

Usage
-----
    # Preview (no file written):
    python backfill_history.py --dry-run

    # Write the first 15 entries:
    python backfill_history.py --count 15

    # Write all entries marked posted_on_pinterest in the inventory:
    python backfill_history.py --posted-only

    # Combine: first 20, preview only:
    python backfill_history.py --count 20 --dry-run

Records written use the format:
    2026-05-04T00:00:00Z | filename.png | MANUAL_IMPORT | MANUAL_IMPORT | Topic

The FilenameHistory class is idempotent -- running this script multiple times
will not create duplicate lines for the same filename.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap project paths
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env", override=True)

import config as cfg
from pinterest_engine.scheduler import FilenameHistory, get_entry_filename

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_MANUAL_TIMESTAMP = "2026-05-04T00:00:00Z"
_MANUAL_TAG       = "MANUAL_IMPORT"


# ---------------------------------------------------------------------------
# Core backfill logic
# ---------------------------------------------------------------------------

def backfill(
    count: int | None = 15,
    posted_only: bool = False,
    dry_run: bool = False,
) -> None:
    inventory_path = cfg.OUTPUTS_DIR / "master_inventory.json"
    if not inventory_path.is_file():
        print(f"ERROR: master_inventory.json not found at {inventory_path}")
        print("Run:  python pinterest_main.py sync  first.")
        sys.exit(1)

    data = json.loads(inventory_path.read_text(encoding="utf-8"))
    entries = data.get("entries", [])

    if not entries:
        print("Inventory is empty. Nothing to backfill.")
        return

    # --- Select entries to register ---
    if posted_only:
        candidates = [
            e for e in entries
            if e.get("publication_status", {}).get("posted_on_pinterest")
        ]
        label = "posted_on_pinterest=True"
    else:
        candidates = entries
        label = "first N entries"

    if count is not None:
        candidates = candidates[:count]

    print(f"\nBackfill History -- {'DRY RUN  ' if dry_run else 'LIVE WRITE'}")
    print(f"  Inventory      : {inventory_path}")
    print(f"  History file   : {cfg.OUTPUTS_DIR / 'scheduled_history.txt'}")
    print(f"  Selection      : {label}")
    print(f"  Entries found  : {len(candidates)}")
    print(f"  Timestamp      : {_MANUAL_TIMESTAMP}")
    print(f"  post_id / pin_id used : {_MANUAL_TAG}")
    print()

    history = FilenameHistory(cfg.OUTPUTS_DIR)
    already = 0
    written = 0
    skipped_no_file = 0

    for entry in candidates:
        post_id  = entry.get("post_id", "UNKNOWN")
        topic    = entry.get("topic", "Holistic Protocol")
        filename = get_entry_filename(entry)

        if not filename or filename == "UNKNOWN":
            print(f"  SKIP (no filename)  {post_id}")
            skipped_no_file += 1
            continue

        if filename in history:
            print(f"  ALREADY in history  {filename}")
            already += 1
            continue

        if dry_run:
            print(f"  [DRY RUN] Would record  {filename}  ({topic[:45]})")
            written += 1
        else:
            history.record(
                filename=filename,
                post_id=post_id,
                pin_id=_MANUAL_TAG,
                topic=topic,
                timestamp=_MANUAL_TIMESTAMP,
            )
            print(f"  Recorded  {filename}  ({topic[:45]})")
            written += 1

    # --- Summary ---
    print()
    print("=" * 55)
    if dry_run:
        print(f"  [DRY RUN] Would write : {written}")
    else:
        print(f"  Written to history   : {written}")
    print(f"  Already present      : {already}")
    print(f"  Skipped (no file)    : {skipped_no_file}")
    if not dry_run and written:
        print(f"\n  History file now has {len(history)} total entries.")
        print(f"  File: {history.path}")
    print()

    if dry_run and written:
        print("Re-run without --dry-run to commit these entries.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manually backfill scheduled_history.txt from master_inventory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--count", "-n",
        type=int,
        default=15,
        help="Number of entries to register (default: 15). Ignored when --posted-only is set.",
    )
    parser.add_argument(
        "--posted-only",
        action="store_true",
        help="Register only entries already marked posted_on_pinterest=True in the inventory.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be written without touching the history file.",
    )
    args = parser.parse_args()

    backfill(
        count=args.count if not args.posted_only else None,
        posted_only=args.posted_only,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
