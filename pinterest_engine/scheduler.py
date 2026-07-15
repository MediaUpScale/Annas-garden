# -*- coding: utf-8 -*-
"""
pinterest_engine.scheduler
---------------------------
Human-Mimic Safe-Drip Scheduler.

Publishes 3-5 pins per session with randomised intervals between each pin
to mimic authentic human posting behaviour (avoids algorithmic spam flags).

Key behaviours:
  - Reads exclusively from master_inventory.json (never posts twice)
  - Checks outputs/scheduled_history.txt BEFORE building each batch so that
    even if master_inventory is rebuilt, no filename is ever re-posted
  - Updates posted_on_pinterest + pinterest_pin_id IMMEDIATELY after success
  - Saves master_inventory.json after EVERY pin (crash-safe)
  - Appends to scheduled_history.txt after every confirmed live publish
  - Sleeps get_random_interval() minutes between pins (real sleep)
  - On 401 token expiry: logs, saves ledger, raises PinterestTokenExpiredError
  - --no-wait flag skips sleep (for testing / dry runs)
  - --dry-run logs without writing to history (history stays clean for tests)

Interval config (in .env):
    MIN_INTERVAL_HOURS  default 3
    MAX_INTERVAL_HOURS  default 6
"""
from __future__ import annotations

import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

log = logging.getLogger(__name__)

_DEFAULT_MIN_HOURS = 3.0
_DEFAULT_MAX_HOURS = 6.0
_DEFAULT_PINS_MIN = 3
_DEFAULT_PINS_MAX = 5
_INTER_PIN_COURTESY_SEC = 3   # brief pause before starting sleep countdown

# ---------------------------------------------------------------------------
# FilenameHistory  --  the filename-level duplicate guard
# ---------------------------------------------------------------------------
# Stored at outputs/scheduled_history.txt.
# Format (one line per published pin):
#   2026-07-03T15:04:00Z | source_filename.jpg | post_id | pin_id | topic
# The *filename* column is what is checked; all other columns are for auditing.

_HISTORY_FILENAME = "scheduled_history.txt"
_HISTORY_SEP = " | "


class FilenameHistory:
    """
    Persistent filename-based guard against accidental re-scheduling.

    Two independent protection layers exist in the engine:
      1. master_inventory.json  ::  posted_on_pinterest flag (per post_id)
      2. scheduled_history.txt  ::  filename set          (per source file)

    This class owns layer 2.  It is intentionally file-based so it
    survives inventory rebuilds, syncs, or any other operation that
    touches master_inventory.json.

    Dry-run sessions do NOT write to this file so test runs never
    consume real history slots.
    """

    def __init__(self, outputs_dir: Path) -> None:
        self.path = outputs_dir / _HISTORY_FILENAME
        self._seen: set[str] = self._load()

    # ------------------------------------------------------------------
    # Public

    def __contains__(self, filename: str) -> bool:
        """True if *filename* has already been scheduled."""
        return filename in self._seen

    def __len__(self) -> int:
        return len(self._seen)

    def record(
        self,
        filename: str,
        post_id: str,
        pin_id: str,
        topic: str,
        timestamp: str | None = None,
    ) -> None:
        """
        Append *filename* to the history file and add it to the in-memory set.

        Parameters
        ----------
        filename  : Source image filename (the deduplication key).
        post_id   : Inventory post_id (or 'MANUAL_IMPORT' for backfills).
        pin_id    : Pinterest pin ID (or 'MANUAL_IMPORT' for backfills).
        topic     : Human-readable topic label for the audit trail.
        timestamp : ISO-8601 UTC string, e.g. '2026-05-04T00:00:00Z'.
                    Defaults to the current UTC time when omitted.
        """
        if filename in self._seen:
            return  # idempotent — safe to call multiple times
        ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = _HISTORY_SEP.join([ts, filename, post_id, pin_id, topic[:60]])
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        self._seen.add(filename)
        log.debug("History: recorded '%s'", filename)

    # ------------------------------------------------------------------
    # Private

    def _load(self) -> set[str]:
        if not self.path.is_file():
            return set()
        seen: set[str] = set()
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(_HISTORY_SEP)
            # Column 1 (0-indexed) is the filename
            if len(parts) >= 2:
                seen.add(parts[1].strip())
        log.debug("History loaded: %d recorded filenames.", len(seen))
        return seen


def get_entry_filename(entry: dict) -> str:
    """
    Return the unique trackable filename for a master_inventory entry.

    Priority:
      1. Basename of local_image_path  (most unique — tied to the file on disk)
      2. Basename of imgbb_url          (public cloud copy)
      3. post_id                        (guaranteed unique fallback)
    """
    local = entry.get("local_image_path", "")
    if local:
        return Path(local).name

    imgbb = entry.get("imgbb_url", "")
    if imgbb:
        # Strip query strings and hash fragments before taking the basename
        return Path(imgbb.split("?")[0].split("#")[0]).name

    return entry.get("post_id", "UNKNOWN")


# ---------------------------------------------------------------------------
# Public scheduling helpers
# ---------------------------------------------------------------------------

def get_random_interval(
    min_hours: float | None = None,
    max_hours: float | None = None,
) -> int:
    """
    Return a random interval in MINUTES between min_hours and max_hours.
    Reads MIN_INTERVAL_HOURS / MAX_INTERVAL_HOURS from env if not passed.
    """
    if min_hours is None:
        try:
            min_hours = float(os.getenv("MIN_INTERVAL_HOURS", str(_DEFAULT_MIN_HOURS)))
        except ValueError:
            min_hours = _DEFAULT_MIN_HOURS
    if max_hours is None:
        try:
            max_hours = float(os.getenv("MAX_INTERVAL_HOURS", str(_DEFAULT_MAX_HOURS)))
        except ValueError:
            max_hours = _DEFAULT_MAX_HOURS

    min_minutes = int(min_hours * 60)
    max_minutes = int(max_hours * 60)
    return random.randint(min_minutes, max_minutes)


def _resolve_pins_per_run() -> int:
    env = os.getenv("PINTEREST_PINS_PER_DAY", "")
    if env.isdigit():
        return int(env)
    return random.randint(_DEFAULT_PINS_MIN, _DEFAULT_PINS_MAX)


def _sleep_with_log(minutes: int, label: str = "") -> None:
    """
    Sleep for `minutes` minutes, logging progress every 10 minutes.
    Can be interrupted cleanly with Ctrl-C.
    """
    total_sec = minutes * 60
    elapsed = 0
    chunk = min(600, total_sec)   # log every 10 min or less

    eta = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.info(
        "Waiting %d min (~%.1f h) before next pin. ETA: %s  %s",
        minutes, minutes / 60, eta, f"[{label}]" if label else "",
    )
    try:
        while elapsed < total_sec:
            time.sleep(chunk)
            elapsed += chunk
            remaining = total_sec - elapsed
            if remaining > 0:
                log.info(
                    "  ... %d min elapsed, %d min remaining.",
                    elapsed // 60, remaining // 60,
                )
    except KeyboardInterrupt:
        log.warning("Sleep interrupted by user (Ctrl-C). Continuing with next pin...")


# ---------------------------------------------------------------------------
# PinterestScheduler
# ---------------------------------------------------------------------------

class PinterestScheduler:
    """
    Orchestrates safe-drip publishing against master_inventory.json.

    Parameters
    ----------
    outputs_dir : Path, optional
        Root outputs/ directory.
    pins_per_run : int, optional
        Override for the number of pins to publish this session.
    dry_run : bool
        Full pipeline except the actual API call.
    no_wait : bool
        Skip the sleep interval (useful for testing).
    min_interval_hours : float, optional
        Override for MIN_INTERVAL_HOURS.
    max_interval_hours : float, optional
        Override for MAX_INTERVAL_HOURS.
    """

    def __init__(
        self,
        outputs_dir: Path | None = None,
        pins_per_run: int | None = None,
        dry_run: bool = False,
        no_wait: bool = False,
        min_interval_hours: float | None = None,
        max_interval_hours: float | None = None,
    ) -> None:
        import config as cfg  # noqa: PLC0415

        self.outputs_dir = outputs_dir or cfg.OUTPUTS_DIR
        self.dry_run = dry_run
        self.no_wait = no_wait
        self.min_interval_hours = min_interval_hours
        self.max_interval_hours = max_interval_hours
        self.pins_per_run = pins_per_run or _resolve_pins_per_run()

        log.info(
            "Scheduler ready: pins_per_run=%d  dry_run=%s  no_wait=%s  "
            "interval=%.1f-%.1fh",
            self.pins_per_run, dry_run, no_wait,
            min_interval_hours or float(os.getenv("MIN_INTERVAL_HOURS", "3")),
            max_interval_hours or float(os.getenv("MAX_INTERVAL_HOURS", "6")),
        )

    # ------------------------------------------------------------------

    def run(self) -> dict:
        """
        Execute one publishing session.

        Returns stats dict: {published, skipped, skipped_by_history, errors, remaining}.
        """
        from pinterest_engine.image_transformer import PinTransformer  # noqa: PLC0415
        from pinterest_engine.inventory import MasterInventory, validate_caption_safe  # noqa: PLC0415
        from pinterest_engine.publisher import (  # noqa: PLC0415
            PinterestPublisher,
            PinterestTokenExpiredError,
        )

        inv = MasterInventory(outputs_dir=self.outputs_dir)
        data = inv.load()

        if not data.get("entries"):
            log.warning(
                "master_inventory.json is empty or missing. "
                "Run: python pinterest_main.py sync"
            )
            return {"published": 0, "skipped": 0, "skipped_by_history": 0,
                    "errors": 0, "remaining": 0}

        # --- Layer 1 guard: inventory posted_on_pinterest flag ---
        unposted = inv.get_unposted(data)

        # --- Layer 2 guard: filename history file ---
        history = FilenameHistory(self.outputs_dir)
        if len(history):
            before = len(unposted)
            unposted = [
                e for e in unposted
                if get_entry_filename(e) not in history
            ]
            skipped_by_history = before - len(unposted)
            if skipped_by_history:
                log.info(
                    "Filename history: skipped %d entr%s already in scheduled_history.txt.",
                    skipped_by_history,
                    "y" if skipped_by_history == 1 else "ies",
                )
        else:
            skipped_by_history = 0

        total_eligible = len(unposted)
        batch = unposted[: self.pins_per_run]

        unposted_before_history = total_eligible + skipped_by_history
        log.info(
            "Inventory: %d total | %d unposted-by-flag | %d blocked-by-history | %d selected",
            len(data["entries"]), unposted_before_history,
            skipped_by_history, len(batch),
        )

        if not batch:
            log.warning(
                "No eligible pins remaining after history filter. "
                "All unposted entries may already be in scheduled_history.txt."
            )
            return {"published": 0, "skipped": 0,
                    "skipped_by_history": skipped_by_history,
                    "errors": 0, "remaining": 0}

        # Initialise tools
        transformer = PinTransformer(outputs_dir=self.outputs_dir)

        publisher = None
        if not self.dry_run:
            try:
                publisher = PinterestPublisher()
                if not publisher.validate_token():
                    from pinterest_engine.publisher import PinterestTokenExpiredError  # noqa: PLC0415
                    raise PinterestTokenExpiredError("Token validation failed before batch.")
            except PinterestTokenExpiredError:
                inv.save(data)
                raise

        stats = {
            "published": 0,
            "skipped": 0,
            "skipped_by_history": skipped_by_history,
            "errors": 0,
            "remaining": total_eligible,
        }

        for i, entry in enumerate(batch):
            post_id = entry["post_id"]
            topic = entry.get("topic", "?")
            title = entry.get("pinterest_title", topic)
            src_filename = get_entry_filename(entry)

            log.info(
                "[%d/%d] Processing: '%s'  post_id=%s  file=%s",
                i + 1, len(batch), topic, post_id, src_filename,
            )

            # --- Safety: validate caption ---
            caption = entry.get("pinterest_caption", "")
            valid, reason = validate_caption_safe(caption)
            if not valid:
                log.warning("  Caption safety check failed (%s) -- auto-fixing.", reason)
                from pinterest_engine.inventory import (  # noqa: PLC0415
                    build_caption_regex, is_raw_draft, strip_draft_headers,
                )
                original = entry.get("original_caption", "")
                fact_sheet = entry.get("raw_fact_sheet", "")

                if is_raw_draft(original):
                    log.warning(
                        "  original_caption is also a raw draft for %s "
                        "-- using raw_fact_sheet as source.", post_id,
                    )
                    source = strip_draft_headers(fact_sheet) if fact_sheet else ""
                else:
                    source = original

                entry["pinterest_caption"] = build_caption_regex(
                    source, entry.get("variant_index", 0)
                )
                log.info(
                    "  Caption rebuilt from %s source.",
                    "fact_sheet" if (is_raw_draft(original) and fact_sheet) else "original_caption",
                )

            # --- Transform image ---
            pin_bytes = transformer.get_pin_bytes(entry)
            if pin_bytes is None:
                log.warning("  No image for %s -- skipping.", post_id)
                stats["skipped"] += 1
                continue

            transformer.transform(entry)   # also save pin to disk for audit

            if self.dry_run:
                log.info(
                    "  [DRY RUN] Would publish '%s' (%d bytes pin image).",
                    title, len(pin_bytes),
                )
                log.info(
                    "  [DRY RUN] Would record '%s' in scheduled_history.txt.",
                    src_filename,
                )
                inv.mark_posted(data, post_id, "DRY_RUN_PIN_ID")
                inv.save(data)
                stats["published"] += 1

            else:
                # --- Live publish ---
                try:
                    result = publisher.publish(entry, pin_bytes)
                except Exception as exc:  # noqa: BLE001
                    from pinterest_engine.publisher import PinterestTokenExpiredError  # noqa: PLC0415
                    if isinstance(exc, PinterestTokenExpiredError):
                        inv.save(data)
                        raise
                    log.error("  Publish exception for %s: %s", post_id, exc)
                    stats["errors"] += 1
                    continue

                if result:
                    pin_id = result.get("id", "unknown")
                    inv.mark_posted(data, post_id, pin_id)
                    inv.save(data)              # crash-safe: save after EVERY success
                    history.record(src_filename, post_id, pin_id, topic)
                    stats["published"] += 1
                    log.info(
                        "  Published! Pin ID=%s | history logged: %s",
                        pin_id, src_filename,
                    )
                else:
                    log.warning("  Soft failure for %s -- NOT recorded in history.", post_id)
                    stats["errors"] += 1

            # --- Human-mimic interval (skip after last pin) ---
            is_last = (i == len(batch) - 1)
            if not is_last and not self.no_wait:
                interval = get_random_interval(
                    self.min_interval_hours, self.max_interval_hours
                )
                time.sleep(_INTER_PIN_COURTESY_SEC)
                _sleep_with_log(interval, label=f"next: {batch[i+1].get('topic', '?')[:40]}")

        stats["remaining"] = max(0, total_eligible - stats["published"])
        log.info(
            "Session done -- published=%d  skipped=%d  history-blocked=%d  "
            "errors=%d  remaining=%d",
            stats["published"], stats["skipped"],
            stats["skipped_by_history"], stats["errors"], stats["remaining"],
        )
        return stats

    # ------------------------------------------------------------------

    def show_status(self) -> None:
        """Print a rich queue/history summary."""
        from pinterest_engine.inventory import MasterInventory  # noqa: PLC0415

        inv = MasterInventory(outputs_dir=self.outputs_dir)
        data = inv.load()
        history = FilenameHistory(self.outputs_dir)

        all_entries = data.get("entries", [])
        total = len(all_entries)

        # --- Compute all counters independently so they never mask each other ---

        # How many entries have posted_on_pinterest=True in the inventory
        posted_flag_count = sum(
            1 for e in all_entries
            if e.get("publication_status", {}).get("posted_on_pinterest")
        )

        # How many inventory entries (across ALL 222) have a filename in the
        # history file — regardless of their posted_on_pinterest status.
        # This is the canonical "Blocked by history" number.
        blocked_by_history = sum(
            1 for e in all_entries
            if get_entry_filename(e) in history
        )

        # True queue = entries that pass BOTH guards:
        #   1. posted_on_pinterest is False  (inventory guard)
        #   2. filename is NOT in history    (filename guard)
        # We use inv.get_unposted() for guard 1 (it also requires image + metadata).
        unposted_inv = inv.get_unposted(data)
        eligible = [
            e for e in unposted_inv
            if get_entry_filename(e) not in history
        ]

        no_img = sum(
            1 for e in all_entries
            if not e.get("local_image_path") and not e.get("imgbb_url")
        )
        no_meta = sum(
            1 for e in all_entries
            if not e.get("pinterest_title")
        )

        print("\nPinterest Engine Status")
        print(f"  Inventory entries      : {total}")
        print(f"  Published (inventory)  : {posted_flag_count}")
        print(f"  History file entries   : {len(history)}")
        print(f"  Blocked by history     : {blocked_by_history}  "
              f"(filenames in {_HISTORY_FILENAME} matched against inventory)")
        print(f"  True queue (eligible)  : {len(eligible)}  "
              f"(unposted + not in history)")
        print(f"  No image data          : {no_img}")
        print(f"  No Pinterest meta      : {no_meta}  (run sync to generate)")

        if eligible:
            print(f"\n  Next {min(5, len(eligible))} in queue:")
            for e in eligible[:5]:
                fn = get_entry_filename(e)
                print(
                    f"    [{e['post_id']}] {e.get('topic', '?')[:40]}"
                    f" | file: {fn}"
                )
        print()
