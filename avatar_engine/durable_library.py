# -*- coding: utf-8 -*-
"""Per-post JSON snapshots under ``outputs/library/`` (pre–humanizer checkpoint + finalize)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PENDING_CAPTION = "PENDING_CAPTION"


def path_under_engine(engine_root: Path, target: Path | str) -> str:
    """Path relative to project root (POSIX strings), e.g. ``outputs/library/post_*.json``."""
    if target is None or str(target).strip() == "":
        return ""
    p = Path(target).expanduser().resolve()
    root = Path(engine_root).resolve()
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix()


def write_atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    obj = json.loads(raw)
    if not isinstance(obj, dict):
        raise ValueError(f"Expected object in {path}, got {type(obj)}")
    return obj


def merge_update_json(path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    payload = load_json(path)
    payload.update(updates)
    write_atomic_json(path, payload)
    return payload
