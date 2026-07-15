# -*- coding: utf-8 -*-
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
env_path = ROOT / ".env"

raw = env_path.read_bytes()
first6 = raw[:6]
print(f"File     : {env_path}")
print(f"Size     : {len(raw)} bytes")
print(f"First 6 bytes (hex): {first6.hex()}")
print(f"UTF-8 BOM    : {raw[:3] == b'xefxbbxbf'.replace(b'x','\\x'.encode())}")

# Proper BOM checks
has_utf8_bom   = raw[:3] == bytes([0xEF, 0xBB, 0xBF])
has_utf16_le   = raw[:2] == bytes([0xFF, 0xFE])
has_utf16_be   = raw[:2] == bytes([0xFE, 0xFF])
has_nul        = b'\x00' in raw

print(f"UTF-8 BOM    : {has_utf8_bom}")
print(f"UTF-16 LE BOM: {has_utf16_le}")
print(f"UTF-16 BE BOM: {has_utf16_be}")
print(f"NUL bytes    : {has_nul}")

# Try loading the key directly
import os
from dotenv import load_dotenv
load_dotenv(env_path, override=True)
key = os.getenv("GEMINI_API_KEY", "")
print(f"\nos.getenv('GEMINI_API_KEY') = {repr(key[:20] + '...' if len(key) > 20 else key)}")
print(f"Truthy: {bool(key)}")

# Also show what first env var name looks like after parsing
for line in raw.decode("utf-8", errors="replace").splitlines()[:3]:
    print(f"  raw line repr: {repr(line[:60])}")
