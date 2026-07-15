# -*- coding: utf-8 -*-
"""Remove NUL (\\x00) bytes from project Python sources; re-save unchanged bytes otherwise.

Google Drive occasionally injects NULs into UTF-8 text files. Run from project root:
    python sanitize_python_sources.py
"""

from __future__ import annotations

from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parent
    fixed: list[Path] = []
    for p in root.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        raw = p.read_bytes()
        if b"\x00" not in raw:
            continue
        p.write_bytes(raw.replace(b"\x00", b""))
        fixed.append(p)
    print(f"Stripped NUL from {len(fixed)} file(s).")
    for f in sorted(fixed, key=lambda x: str(x).lower()):
        print(" ", f.relative_to(root))


if __name__ == "__main__":
    main()
