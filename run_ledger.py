# -*- coding: utf-8 -*-
"""Per-run filesystem log path + optional registry for cross-module notes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

_LOGGER = logging.getLogger(__name__)
_lock = Lock()
_active: "_RunLedger | None" = None


@dataclass
class PlannedModels:
    image_primary_id: str
    research_primary_id: str
    humanizer_summary: str


class _RunLedger:
    """Single active run ledger (attached from ``main``)."""

    __slots__ = ("path", "planned")

    def __init__(self, path: Path, planned: PlannedModels) -> None:
        self.path = path
        self.planned = planned

    def log_planned_banner(self) -> None:
        _LOGGER.info(
            "Run ledger: %s | planned image=%s research=%s humanizer=%s",
            self.path,
            self.planned.image_primary_id,
            self.planned.research_primary_id,
            self.planned.humanizer_summary,
        )


def activate_run_ledger(path: Path, *, planned: PlannedModels) -> Path:
    """Register the ledger for helpers (gemini retries, etc.). Returns ``path``."""
    global _active
    with _lock:
        _active = _RunLedger(path, planned)
        _active.log_planned_banner()
    return path


def deactivate_run_ledger() -> None:
    global _active
    with _lock:
        _active = None


def get_active_ledger() -> _RunLedger | None:
    with _lock:
        return _active


def ledger_file_path(engine_root: Path) -> tuple[Path, str]:
    """Return ``(absolute_log_path, timestamp_token)`` for ``logs/run_<ts>.log``."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    logs = engine_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"run_{ts}.log"
    return path.resolve(), ts


def configure_file_logging(log_path: Path) -> None:
    """Attach UTF-8 file handler (dedup); root level allows INFO entries into run log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    for h in root.handlers:
        if getattr(h, "_engine_run_journal", False):
            return

    fh = logging.FileHandler(log_path, encoding="utf-8", mode="a")
    fh.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    fh.setFormatter(fmt)
    fh._engine_run_journal = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    cur = root.level if root.level != logging.NOTSET else logging.WARNING
    root.setLevel(min(cur, logging.INFO))
