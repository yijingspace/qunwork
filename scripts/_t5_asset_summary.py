# -*- coding: utf-8 -*-
"""t5: summarize asset_usage.json heat distribution + kinds."""
import json, io, sys, collections

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

data = json.load(open(".qunwork/asset_usage/asset_usage.json", encoding="utf-8"))
assets = data["assets"]
print("total assets:", len(assets))
heat = collections.Counter(a["heat"] for a in assets)
print("heat dist:", dict(heat))
kind = collections.Counter(a["kind"] for a in assets)
print("kind dist:", dict(kind))
# by heat and kind
for h in ["HOT", "warm", "cold"]:
    ks = collections.Counter(a["kind"] for a in assets if a["heat"] == h)
    print(h, dict(ks))
# ref_occur / git_touches / db_use_count totals
print("sum ref_occur:", sum(a["ref_occur"] for a in assets))
print("sum git_touches:", sum(a["git_touches"] for a in assets))
print("sum db_use_count:", sum(a["db_use_count"] for a in assets))
print("assets with db_use_count>0:", sum(1 for a in assets if a["db_use_count"] > 0))
print("assets with git_touches>0:", sum(1 for a in assets if a["git_touches"] > 0))
# top assets by git_touches (excluding vendored sidecar internals)
real = [a for a in assets if "sidecar\\_internal" not in a["asset"]]
real.sort(key=lambda a: -a["git_touches"])
print("top git-touched non-vendored assets:")
for a in real[:12]:
    print("  ", a["git_touches"], a["heat"], a["asset"])
# catalog capability assets
caps = [a for a in assets if a["kind"] == "catalog_capability"]
print("catalog capabilities:", len(caps), [(a["asset"], a["ref_occur"]) for a in caps])
# automation artifacts
auto = [a for a in assets if a["kind"] == "automation_artifact"]
print("automation artifacts:", len(auto), [a["asset"] for a in auto])
