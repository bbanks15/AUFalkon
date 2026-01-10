
"""
mission_update.py

Small utility to update mission timing fields:
- tick_ms
- constraints.max_gap_ms

Usage:
  python mission_update.py path/to/mission.json --tick_ms 1 --max_gap_ms 10
"""

import json
import argparse


def update_mission(path: str, tick_ms: float = 1.0, max_gap_ms: int = 10) -> None:
    """Load, update timing fields, and write the mission back to disk."""
    with open(path, "r", encoding="utf-8") as f:
        mission = json.load(f)

    mission["tick_ms"] = float(tick_ms)
    mission.setdefault("constraints", {})
    mission["constraints"]["max_gap_ms"] = int(max_gap_ms)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(mission, f, indent=2)

    print(f"Updated {path}: tick_ms={tick_ms}, max_gap_ms={max_gap_ms}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--tick_ms", type=float, default=1.0)
    ap.add_argument("--max_gap_ms", type=int, default=10)
    args = ap.parse_args()

    update_mission(args.path, args.tick_ms, args.max_gap_ms)
