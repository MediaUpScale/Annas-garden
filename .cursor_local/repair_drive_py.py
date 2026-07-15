# -*- coding: utf-8 -*-
"""Run from LOCAL Cursor copy (NOT Google Drive) to sanitize UTF-16 / NUL corrupted .py files.

Usage:
  python .cursor_local/repair_drive_py.py "G:\\path\\to\\file.py"

If no paths given, sanitizes sibling folder main.py and repair_python_utf8.py under cwd.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def normalize_bytes(data: bytes) -> str:
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le", errors="replace").lstrip("\ufeff")
    if data.startswith(b"\xfe\xff"):
        return data[2:].decode("utf-16-be", errors="replace").lstrip("\ufeff")
    return data.replace(b"\x00", b"").decode("utf-8", errors="replace")


def write_utf8_py(path: Path) -> dict[str, int | str]:
    path = path.expanduser().resolve()
    raw = path.read_bytes()
    before_nulls = raw.count(b"\x00")
    utf16 = raw.startswith((b"\xff\xfe", b"\xfe\xff"))

    text = normalize_bytes(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text and not text.endswith("\n"):
        text += "\n"

    path.write_bytes(text.encode("utf-8"))
    return {"path": str(path), "nul_bytes_removed": before_nulls, "utf16_bom": int(utf16)}


def main(argv: list[str]) -> int:
    paths = argv[1:]
    cwd = Path.cwd()

    default_files = []
    mp = cwd / "main.py"
    if mp.exists():
        default_files.append(mp)
    rep = cwd / "repair_python_utf8.py"
    if rep.exists():
        default_files.append(rep)

    if not paths:
        if not default_files:
            print("No paths given and main.py missing in cwd.", file=sys.stderr)
            return 1
        targets = default_files
    else:
        targets = [Path(p) for p in paths]

    for p in targets:
        if not p.is_file():
            print(f"[skip] not found: {p}", file=sys.stderr)
            continue
        info = write_utf8_py(p)
        print(
            "[ok]",
            info["path"],
            "| nul_bytes:",
            info["nul_bytes_removed"],
            "| had_utf16_bom:",
            info["utf16_bom"],
        )

    env_copy = os.environ.get("SANITIZE_G_MAIN")
    if env_copy:
        g_main = Path(env_copy)
        if g_main.is_file():
            info = write_utf8_py(g_main)
            print("[ok-g]", info["path"], "| nuls:", info["nul_bytes_removed"])
        else:
            print("[skip-g] SANITIZE_G_MAIN not found:", env_copy, file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
