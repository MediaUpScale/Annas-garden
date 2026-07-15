# -*- coding: utf-8 -*-
"""Confirm the scheduler's caption-fix block works on actual raw-draft entries."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)

from pinterest_engine.inventory import (
    MasterInventory, validate_caption_safe, is_raw_draft,
    build_caption_regex, strip_draft_headers,
)

inv = MasterInventory()
data = inv.load()
entries = data.get("entries", [])

raw_draft_entries = [
    e for e in entries
    if is_raw_draft(e.get("pinterest_caption", ""))
]

print(f"\nEntries with raw-draft pinterest_caption: {len(raw_draft_entries)}")

if not raw_draft_entries:
    print("None found -- inventory is already clean.")
else:
    print("\nSimulating scheduler auto-fix on first 3:")
    for e in raw_draft_entries[:3]:
        post_id = e["post_id"]
        topic   = e.get("topic", "?")
        bad_cap = e.get("pinterest_caption", "")

        original = e.get("original_caption", "")
        fact_sheet = e.get("raw_fact_sheet", "")

        if is_raw_draft(original):
            source = strip_draft_headers(fact_sheet) if fact_sheet else ""
            src_label = "fact_sheet"
        else:
            source = original
            src_label = "original_caption"

        fixed = build_caption_regex(source, e.get("variant_index", 0))
        ok, reason = validate_caption_safe(fixed)

        print(f"\n  [{post_id}] {topic[:45]}")
        print(f"    Source used   : {src_label}")
        print(f"    Fixed passes  : {ok} ({reason})")
        print(f"    Has raw tag   : {is_raw_draft(fixed)}")
        print(f"    Preview       : {fixed[:100].replace(chr(10),' ')!r}")

print()
