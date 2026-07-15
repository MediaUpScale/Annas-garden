# -*- coding: utf-8 -*-
"""Quick duplicate / overlap audit for master_inventory.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
inv_path = ROOT / "outputs" / "master_inventory.json"
data = json.loads(inv_path.read_text(encoding="utf-8"))
entries = data.get("entries", [])

ids = [e.get("post_id", "") for e in entries]
dupes = [x for x in set(ids) if ids.count(x) > 1 and x]
if dupes:
    print(f"WARNING -- {len(dupes)} duplicate post_ids: {dupes[:5]}")
else:
    print(f"OK  -- {len(ids)} entries, 0 duplicate post_ids")

pin_posted = [e for e in entries if e.get("publication_status", {}).get("posted_on_pinterest")]
ig_queued  = [e for e in entries if e.get("publication_status", {}).get("posted_on_instagram")]
overlap    = set(e["post_id"] for e in pin_posted) & set(e["post_id"] for e in ig_queued)

print(f"Pinterest posted : {len(pin_posted)}")
print(f"Instagram/FB queued (post-export) : {len(ig_queued)}")
if overlap:
    print(f"WARNING -- {len(overlap)} entries flagged on BOTH channels (should be expected for dual-platform).")
else:
    print("No cross-contamination between Pinterest and Post Planner queues.")
print("Audit complete.")
