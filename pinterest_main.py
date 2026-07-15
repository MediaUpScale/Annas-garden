# -*- coding: utf-8 -*-
"""
pinterest_main.py
-----------------
Pinterest Sales & Recycling Engine -- Master CLI (Standard Production access).

WORKFLOW (in order):
  1. sync              -- Build master_inventory.json, repair paths, inject metadata.
  2. schedule          -- Publish N pins with human-mimic random intervals.
  3. export            -- Generate Post Planner bulk-upload CSV (Facebook/multi-channel).
  4. transform         -- Preview a single 2:3 pin image (no publish).
  5. status            -- Queue stats + publish history.
  6. check-readiness   -- Pre-flight checklist.
  7. validate-token    -- Quick token health check.

QUICK START:
    python pinterest_main.py sync                    # build/refresh inventory
    python pinterest_main.py check-readiness         # confirm all green
    python pinterest_main.py schedule --quantity 5   # publish 5 pins today
    python pinterest_main.py export --posts-per-day 3  # generate Facebook CSV
    python pinterest_main.py status                  # monitor queue
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import config  # noqa: E402  -- loads .env

# Unicode-safe logging: Windows CP1252 console breaks on non-Latin chars
# (emoji in imgbb URLs, etc.). Replace unencodable chars with '?' instead of crashing.
_safe_stream = io.TextIOWrapper(
    sys.stdout.buffer,
    encoding=sys.stdout.encoding or "utf-8",
    errors="replace",
    line_buffering=True,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(_safe_stream)],
)
log = logging.getLogger("pinterest_main")


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_sync(args: argparse.Namespace) -> None:
    """
    Phase 1: Build/update master_inventory.json.
    Merges all library JSONs + content_library, repairs G: Drive image paths,
    and generates Pinterest-specific metadata for every entry.
    """
    from pinterest_engine.inventory import MasterInventory  # noqa: PLC0415

    print("\n[SYNC] Building Master Inventory...")
    print(f"  AI mode   : {'Claude' if not args.no_ai else 'Regex/Template'}")
    print(f"  Limit     : {args.limit or 'all'}")
    print(f"  Force     : {args.force}")
    print(f"  Dry run   : {args.dry_run}\n")

    inv = MasterInventory()
    data = inv.build(
        use_ai=not args.no_ai,
        ai_delay_sec=args.ai_delay,
        force_regenerate=args.force,
        limit=args.limit,
        dry_run=args.dry_run,
    )

    total = len(data.get("entries", []))
    with_meta = sum(1 for e in data.get("entries", []) if e.get("pinterest_title"))
    verified = inv.count_verified_images(data)
    unposted = len(inv.get_unposted(data))

    print(f"\nSync complete:")
    print(f"  Total entries        : {total}")
    print(f"  With Pinterest meta  : {with_meta}")
    print(f"  Verified local imgs  : {verified}")
    print(f"  Queue (unposted)     : {unposted}")
    if not args.dry_run:
        print(f"  Saved to             : {inv.inventory_path}")
    print()


def cmd_schedule(args: argparse.Namespace) -> None:
    """
    Phase 2: Human-mimic safe-drip publisher.
    Posts N pins with randomised intervals between each.
    """
    from pinterest_engine.publisher import PinterestTokenExpiredError  # noqa: PLC0415
    from pinterest_engine.scheduler import PinterestScheduler  # noqa: PLC0415

    qty = args.quantity
    print(f"\n[SCHEDULE] Starting safe-drip session: {qty} pins")
    print(f"  Interval  : {args.min_hours or 3}-{args.max_hours or 6} hours between pins")
    print(f"  Dry run   : {args.dry_run}")
    print(f"  No wait   : {args.no_wait}")
    print()

    scheduler = PinterestScheduler(
        pins_per_run=qty,
        dry_run=args.dry_run,
        no_wait=args.no_wait,
        min_interval_hours=args.min_hours,
        max_interval_hours=args.max_hours,
    )

    try:
        stats = scheduler.run()
    except PinterestTokenExpiredError as exc:
        print(
            f"\nERROR: {exc}\n"
            "\nACTION REQUIRED:\n"
            "  1. Go to https://developers.pinterest.com/ and generate a new token.\n"
            "  2. Update PINTEREST_ACCESS_TOKEN in your .env file.\n"
            "  3. Re-run: python pinterest_main.py schedule\n"
        )
        sys.exit(2)

    print(
        f"\nSession complete.\n"
        f"  Published : {stats['published']}\n"
        f"  Skipped   : {stats['skipped']}\n"
        f"  Errors    : {stats['errors']}\n"
        f"  Remaining : {stats['remaining']} pins in queue\n"
    )


def cmd_transform(args: argparse.Namespace) -> None:
    """Preview a single 2:3 pin transformation (no publish)."""
    from pinterest_engine.image_transformer import PinTransformer  # noqa: PLC0415
    from pinterest_engine.inventory import MasterInventory  # noqa: PLC0415

    inv = MasterInventory()
    data = inv.load()

    if not data.get("entries"):
        print("master_inventory.json is empty. Run: python pinterest_main.py sync")
        sys.exit(1)

    if args.post_id:
        entry = inv.get_entry(data, args.post_id)
        if entry is None:
            print(f"Post ID '{args.post_id}' not found in master inventory.")
            sys.exit(1)
    else:
        # Pick the first unposted entry with image data
        candidates = [
            e for e in data["entries"]
            if (e.get("local_image_path") or e.get("imgbb_url"))
            and not e.get("publication_status", {}).get("posted_on_pinterest")
        ]
        if not candidates:
            print("No unposted entries with image data found.")
            sys.exit(1)
        entry = candidates[0]

    print(f"\n[TRANSFORM] Building 2:3 pin for:")
    print(f"  Topic       : {entry.get('topic', '?')}")
    print(f"  Visual hook : {entry.get('visual_hook', '(not set)')}")
    print(f"  Title       : {entry.get('pinterest_title', '(not set)')}")
    print(f"  Method      : {args.method}\n")

    transformer = PinTransformer(method=args.method)
    result = transformer.transform(entry)

    if result:
        print(f"Pin saved: {result}")
        if args.open:
            import subprocess  # noqa: PLC0415
            subprocess.run(["explorer", str(result)], check=False)
    else:
        print("Transform failed -- no image source found for this entry.")


def cmd_status(args: argparse.Namespace) -> None:
    """Show queue stats and last published pins."""
    from pinterest_engine.scheduler import PinterestScheduler  # noqa: PLC0415

    scheduler = PinterestScheduler()
    scheduler.show_status()

    history_path = config.OUTPUTS_DIR / "pinterest_history.json"
    if history_path.is_file():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
            published = history.get("published", [])
            if published:
                print(f"  Last {min(5, len(published))} legacy-ledger pins:")
                for e in published[-5:]:
                    print(
                        f"    [{e.get('published_utc', '?')[:19]}] "
                        f"Pin {e.get('pinterest_pin_id', '?')} "
                        f"-- {e.get('title', '?')[:55]}"
                    )
                print()
        except Exception:  # noqa: BLE001
            pass


def cmd_check_readiness(args: argparse.Namespace) -> None:
    """Run the pre-flight checklist."""
    from pinterest_engine.inventory import MasterInventory  # noqa: PLC0415

    inv = MasterInventory()
    checks = inv.check_readiness()
    sys.exit(0 if checks.get("ready") else 1)


def cmd_validate_token(args: argparse.Namespace) -> None:
    """Quick Pinterest access token health check (auto-refresh on 401)."""
    from pinterest_engine.publisher import PinterestPublisher  # noqa: PLC0415

    print("\n[TOKEN CHECK] Validating Pinterest access token...")
    try:
        pub = PinterestPublisher()
        valid = pub.validate_token()
        if valid:
            print("Token is VALID. Ready to publish.\n")
        else:
            print(
                "Token is INVALID or EXPIRED.\n"
                "Run: python pinterest_oauth.py  to re-authenticate.\n"
            )
            sys.exit(1)
    except ValueError as exc:
        print(f"Configuration error: {exc}\n")
        sys.exit(1)


def cmd_export(args: argparse.Namespace) -> None:
    """Generate a Post Planner bulk-upload CSV for Facebook / multi-channel scheduling."""
    from postplanner_exporter import export_to_csv  # noqa: PLC0415

    from datetime import date as _date  # noqa: PLC0415

    start: _date | None = None
    if args.start:
        from datetime import datetime as _dt  # noqa: PLC0415
        start = _dt.strptime(args.start, "%Y-%m-%d").date()

    print("\n[EXPORT] Generating Post Planner CSV...")
    print(f"  Posts per day : {args.posts_per_day}")
    print(f"  Limit         : {args.limit or 'all unscheduled'}")
    print(f"  Start date    : {args.start or 'tomorrow'}")
    print(f"  Mark exported : {args.mark_exported}")
    print(f"  Dry run       : {args.dry_run}\n")

    result = export_to_csv(
        limit=args.limit,
        posts_per_day=args.posts_per_day,
        slots=args.slots or None,
        start_date=start,
        mark_exported=args.mark_exported,
        dry_run=args.dry_run,
    )

    if result and args.open:
        import subprocess  # noqa: PLC0415
        subprocess.run(["explorer", str(result.parent)], check=False)


# ---------------------------------------------------------------------------
# CLI definition
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pinterest_main",
        description="Pinterest Sales & Recycling Engine -- Standard Production.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- sync ---
    p_sync = sub.add_parser(
        "sync",
        help="Build master_inventory.json: merge library, repair paths, inject metadata.",
    )
    p_sync.add_argument("--limit", type=int, default=None,
                        help="Max entries to process (default: all).")
    p_sync.add_argument("--force", action="store_true",
                        help="Re-generate metadata even if it already exists.")
    p_sync.add_argument("--dry-run", action="store_true",
                        help="Preview without writing.")
    p_sync.add_argument("--no-ai", action="store_true",
                        help="Use fast regex/template mode instead of Claude.")
    p_sync.add_argument("--ai-delay", type=float, default=0.8,
                        help="Seconds between Claude API calls (default: 0.8).")

    # --- schedule ---
    p_sched = sub.add_parser(
        "schedule",
        help="Publish N pins live to Pinterest with human-mimic intervals.",
    )
    p_sched.add_argument("--quantity", "-n", type=int, default=None,
                         help="Number of pins to publish (default: random 3-5).")
    p_sched.add_argument("--min-hours", type=float, default=None,
                         help="Min hours between pins (default: 3).")
    p_sched.add_argument("--max-hours", type=float, default=None,
                         help="Max hours between pins (default: 6).")
    p_sched.add_argument("--dry-run", action="store_true",
                         help="Full pipeline without calling the Pinterest API.")
    p_sched.add_argument("--no-wait", action="store_true",
                         help="Skip sleep intervals (testing only).")

    # --- export ---
    p_exp = sub.add_parser(
        "export",
        help="Export unscheduled posts to Post Planner bulk-upload CSV.",
    )
    p_exp.add_argument("--limit", type=int, default=None,
                       help="Max entries to include (default: all unscheduled).")
    p_exp.add_argument("--posts-per-day", type=int, default=3,
                       help="Posts per day in the schedule (default: 3).")
    p_exp.add_argument("--slots", nargs="+", default=None, metavar="HH:MM",
                       help="Custom posting times e.g. --slots 08:00 13:00 18:00")
    p_exp.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                       help="First posting date (default: tomorrow).")
    p_exp.add_argument("--mark-exported", action="store_true",
                       help="Set posted_on_instagram=True in master_inventory after export.")
    p_exp.add_argument("--dry-run", action="store_true",
                       help="Preview without writing the CSV.")
    p_exp.add_argument("--open", action="store_true",
                       help="Open the output folder in Explorer after export.")

    # --- transform ---
    p_tf = sub.add_parser("transform", help="Preview a 2:3 pin image (no publish).")
    p_tf.add_argument("--post-id", default=None,
                      help="Specific post_id from master_inventory.")
    p_tf.add_argument("--method", choices=["blurred_padding", "center_crop"],
                      default="blurred_padding")
    p_tf.add_argument("--open", action="store_true",
                      help="Open the output file in Windows Explorer.")

    # --- status ---
    sub.add_parser("status", help="Show queue and publish history summary.")

    # --- check-readiness ---
    sub.add_parser("check-readiness", help="Pre-flight checklist before first publish.")

    # --- validate-token ---
    sub.add_parser("validate-token", help="Test token validity (auto-refresh if expired).")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    dispatch = {
        "sync": cmd_sync,
        "schedule": cmd_schedule,
        "export": cmd_export,
        "transform": cmd_transform,
        "status": cmd_status,
        "check-readiness": cmd_check_readiness,
        "validate-token": cmd_validate_token,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
