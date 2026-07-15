# -*- coding: utf-8 -*-
"""Unit-test the caption filter pipeline end-to-end."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pinterest_engine.inventory import (
    is_raw_draft,
    strip_draft_headers,
    build_caption_regex,
    validate_caption_safe,
)

PASS = "[PASS]"
FAIL = "[FAIL]"

def check(label, cond):
    icon = PASS if cond else FAIL
    print(f"  {icon} {label}")
    return cond

results = []

# --- is_raw_draft ---
print("\n=== is_raw_draft ===")
results.append(check("Detects '[Caption from researcher output...]'",
    is_raw_draft("[Caption from researcher output - humanizer skipped]\n\n**RAW FACT SHEET**")))
results.append(check("Detects '[Researcher draft   humanizer unavailable]'",
    is_raw_draft("[Researcher draft   humanizer unavailable]\n\nSome content")))
results.append(check("Detects standalone **RAW FACT SHEET**",
    is_raw_draft("**RAW FACT SHEET**\n\n- Point one")))
results.append(check("Does NOT flag clean caption",
    not is_raw_draft("Celtic salt carries 82 trace minerals that activate the sodium-potassium pump.")))
results.append(check("Does NOT flag empty string",
    not is_raw_draft("")))

# --- strip_draft_headers ---
print("\n=== strip_draft_headers ===")
raw = (
    "[Caption from researcher output - humanizer skipped]\n\n"
    "**RAW FACT SHEET**\n\n"
    "**1. Verified Mechanisms:**\n"
    "- Celtic salt activates the sodium-potassium pump.\n\n"
    "**2. The Core Protocol:**\n"
    "- Place one grain on the tongue before morning water.\n"
)
stripped = strip_draft_headers(raw)
results.append(check("Removes bracket tag", "[Caption from researcher" not in stripped))
results.append(check("Removes **RAW FACT SHEET**", "**RAW FACT SHEET**" not in stripped))
results.append(check("Removes numbered markdown headers", "**1. Verified Mechanisms:**" not in stripped))
results.append(check("Keeps science bullet content", "Celtic salt activates" in stripped))
results.append(check("Keeps protocol bullet content", "one grain on the tongue" in stripped))

# --- build_caption_regex on raw draft source ---
print("\n=== build_caption_regex (raw draft input) ===")
caption = build_caption_regex(raw, variant_idx=0)
results.append(check("Output does not contain bracket tag", "[Caption from researcher" not in caption))
results.append(check("Output does not contain RAW FACT SHEET", "**RAW FACT SHEET**" not in caption))
results.append(check("Output contains sales URL", "blueprint.holisticprotocolslab.com" in caption))
results.append(check("Output is non-empty", len(caption.strip()) > 30))
print(f"  Built caption preview: {caption[:120].replace(chr(10), ' ')!r}")

# --- validate_caption_safe ---
print("\n=== validate_caption_safe ===")
bad_raw = "[Caption from researcher output]\n\n**RAW FACT SHEET**\n\nSome content.\n\nhttp://blueprint.holisticprotocolslab.com/"
valid, reason = validate_caption_safe(bad_raw)
results.append(check(f"Raw draft is rejected ({reason})", not valid))

good_caption = (
    "Celtic salt carries 82 trace minerals that activate the sodium-potassium pump. "
    "Place one grain on your tongue before morning water and your cells absorb it properly.\n\n"
    "The full protocol for cellular reset is linked in this pin. http://blueprint.holisticprotocolslab.com/"
)
valid, reason = validate_caption_safe(good_caption)
results.append(check(f"Clean caption passes ({reason})", valid))

# --- Summary ---
total = len(results)
passed = sum(results)
print(f"\n{'='*45}")
print(f"  {passed}/{total} checks passed")
if passed == total:
    print("  ALL PASS -- caption filter is working correctly.")
else:
    print(f"  {total-passed} FAILURES -- see above.")
print()
