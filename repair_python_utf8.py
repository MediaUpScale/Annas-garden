# -*- coding: utf-8 -*-
"""
Fix Python source saved as UTF-16 or with embedded NUL bytes (common with Google Drive / bad encodings).

Also normalizes to UTF-8 without BOM and forces the PEP 263 UTF-8 declaration as line 1 (after shebang if present).

Usage (from project folder):
  python repair_python_utf8.py main.py
  python repair_python_utf8.py "G:\\My Drive\\...\\main.py"

Or repair several files:
  python repair_python_utf8.py main.py config.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_UTF8_COOKIE = "# -*- coding: utf-8 -*-"


def _normalize_body_after_decode(text: str) -> str:
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and lines[0].startswith("#!"):
        head = [lines.pop(0)]
        while lines and not lines[0].strip():
            lines.pop(0)
        if lines and "coding" in lines[0] and lines[0].lstrip().startswith("#"):
            lines.pop(0)
        body_lines = head + lines
    else:
        if lines and "coding" in lines[0] and lines[0].lstrip().startswith("#"):
            lines.pop(0)
        body_lines = lines
    body = "\n".join(body_lines).lstrip("\n")
    if body and not body.endswith("\n"):
        body += "\n"
    return body


def repair_file(path: Path) -> str:
    path = path.expanduser().resolve()
    raw = path.read_bytes()

    if raw.startswith(b"\xff\xfe"):
        text = raw[2:].decode("utf-16-le", errors="replace")
    elif raw.startswith(b"\xfe\xff"):
        text = raw[2:].decode("utf-16-be", errors="replace")
    else:
        cleaned = raw.replace(b"\x00", b"")
        text = cleaned.decode("utf-8", errors="replace")

    body = _normalize_body_after_decode(text)
    if body.startswith("#!"):
        first_line, _, rest = body.partition("\n")
        out = first_line + "\n" + _UTF8_COOKIE + "\n" + rest
    else:
        out = _UTF8_COOKIE + "\n" + body
    path.write_bytes(out.encode("utf-8"))
    return str(path)


def main(argv: list[str]) -> int:
    targets = argv[1:] or ["main.py"]
    for name in targets:
        p = Path(name)
        if not p.is_file():
            print(f"[skip] not found: {p}", file=sys.stderr)
            continue
        out = repair_file(p)
        print(f"[ok] rewritten as UTF-8 (NUL/UTF-16 normalized, cookie line 1): {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
