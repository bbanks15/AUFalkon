"""src/mission_update.py

Utility to update mission timing fields and (optionally) enforce simulator-required structure.

Updates:
- tick_ms
- constraints.max_gap_ms

Optional structure helpers:
- --ensure_rest : ensures mission.domains includes 'rest' (required by simulator). If missing, appends it.

Usage:
  python src/mission_update.py path/to/mission.json --tick_ms 1 --max_gap_ms 10
  python src/mission_update.py path/to/mission.json --tick_ms 5 --max_gap_ms 100 --ensure_rest

Note:
- This script makes in-place edits. Use --backup to write a timestamped .bak copy first.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from typing import Any, Dict


def ensure_rest_domain(mission: Dict[str, Any]) -> bool:
    """Ensure 'rest' appears in mission['domains']. Returns True if modified."""
    domains = mission.get("domains")
    if not isinstance(domains, list):
        return False
    has_rest = any(str(d).lower() == "rest" for d in domains)
    if has_rest:
        return False
    domains.append("rest")
    mission["domains"] = domains
    return True


def update_mission(path: str, tick_ms: float = 1.0, max_gap_ms: int = 10, ensure_rest: bool = False, backup: bool = False) -> None:
    """Load, update timing fields, and write mission back to disk."""
    if backup:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = f"{path}.bak_{ts}"
        shutil.copyfile(path, bak)

    with open(path, "r", encoding="utf-8") as f:
        mission = json.load(f)

    mission["tick_ms"] = float(tick_ms)
    mission.setdefault("constraints", {})
    mission["constraints"]["max_gap_ms"] = int(max_gap_ms)

    changed = False
    if ensure_rest:
        changed = ensure_rest_domain(mission) or changed

    with open(path, "w", encoding="utf-8") as f:
        json.dump(mission, f, indent=2)

    extra = " +rest" if (ensure_rest and changed) else ""
    print(f"Updated {path}: tick_ms={tick_ms}, max_gap_ms={max_gap_ms}{extra}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tick_ms", type=float, default=1.0)
    ap.add_argument("--max_gap_ms", type=int, default=10)
    ap.add_argument("--ensure_rest", action="store_true", help="Ensure mission.domains includes 'rest' (append if missing)")
    ap.add_argument("--backup", action="store_true", help="Write a timestamped .bak copy before modifying")
    args = ap.parse_args()

    update_mission(args.path, args.tick_ms, args.max_gap_ms, ensure_rest=args.ensure_rest, backup=args.backup)
