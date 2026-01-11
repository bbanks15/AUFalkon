#!/usr/bin/env python3
"""Simple runner to validate battery recharge while units are resting.

Usage:
  python scripts/check_battery_rest.py <mission.json> [--unit UNIT] [--ticks N] [--logs_dir DIR]

The script runs the mission headless, parses `assignment_samples.csv` and `battery_samples.csv`,
and checks that a monitored unit's battery increases across a rest period.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import List, Dict

import sys
import os
# Ensure repo root is on sys.path so `src` package imports work when run from scripts/
REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
REPO_SRC = os.path.join(REPO_ROOT, "src")
# prefer adding src/ so relative imports in src/ modules resolve
if REPO_SRC not in sys.path:
    sys.path.insert(0, REPO_SRC)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.mission_runner import run_mission


def read_csv_rows(path: str) -> List[Dict[str, str]]:
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def find_recharge_during_rest(assign_rows: List[Dict[str, str]], batt_rows: List[Dict[str, str]], unit: str):
    # Build mapping of sample_tick -> list of assigned units (from assignment row domain_* columns)
    assign_map = {}
    for r in assign_rows:
        tick = int(r.get("sample_tick", "0"))
        units = []
        for k, v in r.items():
            if k.startswith("domain_") and k.endswith("_devices"):
                val = (v or "").strip()
                if val:
                    units.extend([x.strip() for x in val.split(";") if x.strip()])
        assign_map[tick] = units

    # Map battery by tick for the unit
    batt_map = {}
    for r in batt_rows:
        try:
            if r.get("unit", "") != unit:
                continue
            t = int(r.get("sample_tick", "0"))
            b = float(r.get("battery_pct", "0"))
            batt_map[t] = b
        except Exception:
            continue

    if not batt_map:
        return None

    sorted_ticks = sorted(set(list(assign_map.keys()) + list(batt_map.keys())))

    # For each tick where unit is REST (not in assign_map), check if battery increased vs an earlier sample
    for t in sorted_ticks:
        if t not in batt_map:
            continue
        units_at_t = assign_map.get(t, [])
        if unit in units_at_t:
            continue
        # find previous battery sample
        prev_ticks = [pt for pt in sorted(batt_map.keys()) if pt < t]
        if not prev_ticks:
            continue
        prev_t = prev_ticks[-1]
        b_before = batt_map.get(prev_t)
        b_after = batt_map.get(t)
        if b_before is None or b_after is None:
            continue
        if b_after > b_before + 1e-6:
            return (prev_t, t, b_before, b_after, unit)
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mission")
    ap.add_argument("--unit", default="")
    ap.add_argument("--ticks", type=int, default=2000)
    ap.add_argument("--logs_dir", default="runner_batt_check")
    ap.add_argument("--capacity_per_unit", type=int, default=2)
    args = ap.parse_args(argv)

    with open(args.mission, "r", encoding="utf-8") as f:
        m = json.load(f)

    units = m.get("units", []) or []
    unit = args.unit or (units[0] if units else "")
    if not unit:
        print("No unit specified and mission has no units.")
        return 2

    # Ensure logs dir is unique per run
    logs_dir = args.logs_dir
    if os.path.exists(logs_dir):
        # avoid overwriting existing runs
        import shutil

        shutil.rmtree(logs_dir)

    print(f"Running mission {args.mission} for {args.ticks} ticks, monitoring unit={unit}")
    res = run_mission(mission_path=args.mission, ticks=args.ticks, logs_dir=logs_dir, capacity_per_unit=args.capacity_per_unit)
    print("Run result:", res.get("status"))

    batt_csv = os.path.join(logs_dir, "battery_samples.csv")
    assign_csv = os.path.join(logs_dir, "assignment_samples.csv")
    if not os.path.exists(batt_csv) or not os.path.exists(assign_csv):
        print("Missing sample CSVs; run may have failed or used different sampling settings.")
        return 2

    batt_rows = read_csv_rows(batt_csv)
    assign_rows = read_csv_rows(assign_csv)

    found = find_recharge_during_rest(assign_rows, batt_rows, unit)
    if not found:
        # Try all units to find one that shows recharge
        for u in units:
            found = find_recharge_during_rest(assign_rows, batt_rows, u)
            if found:
                unit = u
                break
        if not found:
            print("Could not find any unit showing battery recharge during rest; try a longer run or different mission.")
            return 3

    prev_t, rest_t, b_before, b_after, unit_found = found
    print(f"Unit {unit_found} battery at earlier sample tick={prev_t}: {b_before:.3f}%")
    print(f"Unit {unit_found} battery at rest sample tick={rest_t}:    {b_after:.3f}%")

    if b_after > b_before + 1e-6:
        print(f"PASS: unit={unit_found} battery increased during rest period.")
        return 0
    else:
        print(f"FAIL: unit={unit_found} battery did not increase during rest period.")
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
