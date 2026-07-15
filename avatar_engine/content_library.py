# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_library(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def save_library(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_entry(path: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Append ``entry`` verbatim."""
    rows = load_library(path)
    rows.append(entry)
    save_library(path, rows)
    return entry


def build_library_metadata(
    *,
    topic: str,
    final_caption: str,
    imgbb_url: str,
) -> dict[str, Any]:
    """Lean library row: only the three fields needed by the scheduler."""
    return {
        "topic": topic,
        "final_caption": final_caption,
        "imgbb_url": imgbb_url,
    }


def dump_raw_research_to_log(
    logs_dir: Path,
    *,
    run_stamp: str,
    topic: str,
    variant_index: int,
    raw_fact_sheet: str,
) -> Path:
    """
    Write the full researcher fact sheet to ``logs/research_<run_stamp>_v<n>.txt``
    so it never bloats the lean library JSON.
    """
    logs_dir = Path(logs_dir).expanduser().resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"research_{run_stamp}_v{variant_index:02d}.txt"
    header = f"Topic: {topic}\nVariant: {variant_index}\nTimestamp: {_utc_now_iso()}\n\n"
    log_path.write_text(header + (raw_fact_sheet or ""), encoding="utf-8")
    return log_path
