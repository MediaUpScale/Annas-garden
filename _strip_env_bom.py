# -*- coding: utf-8 -*-
"""Strip UTF-8 BOM from .env and report result."""
from pathlib import Path

env = Path(__file__).resolve().parent / ".env"
raw = env.read_bytes()

UTF8_BOM = bytes([0xEF, 0xBB, 0xBF])
if raw[:3] == UTF8_BOM:
    env.write_bytes(raw[3:])
    print(f"BOM stripped from {env}")
else:
    print(f"No BOM found in {env} -- already clean.")

# Verify
raw2 = env.read_bytes()
print(f"First 6 bytes now: {raw2[:6].hex()}  (should start with 47454d = 'GEM')")
