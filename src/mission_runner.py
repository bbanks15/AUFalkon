
"""
mission_runner.py

Runs a mission through DeadlineScheduler for a number of ticks (headless simulation).

Used by CI gating to ensure:
- Scheduler can meet per-domain requirements
- Hard constraints are not violated

Supports:
- --initial_faults N: permanently fault the first N units (alphabetical, deterministic)

Output:
- JSON dict: {"status":"PASS"/"FAIL", "error":"...", "run_summary":{...}}
"""

import json
import argparse
from typing import Dict, Any, List

from scheduler_deadline import DeadlineScheduler


def run_mission(
    mission_path: str,
    ticks: int,
    logs_dir: str,
    capacity_per_unit: int = 2,
    initial_faults: int = 0,
) -> Dict[str, Any]:
    """Run mission for the given number of ticks."""
    with open(mission_path, "r", encoding="utf-8") as f:
        mission = json.load(f)

    tick_ms = float(mission.get("tick_ms", 1.0))
    max_gap_ms = int(mission["constraints"]["max_gap_ms"])
    max_gap_ticks = max(1, int(max_gap_ms / tick_ms))

    domains: List[str] = mission["domains"]
    units: List[str] = mission["units"]
    required_map = mission.get("required_active_per_domain", {d: 1 for d in domains})
    pools = {d: mission.get("domain_pools", {}).get(d, []) for d in domains}
    pools["spares"] = mission.get("domain_pools", {}).get("spares", [])

    universal_roles = bool(mission.get("universal_roles", True))
    domain_weights = mission.get("domain_weights", {}) if isinstance(mission.get("domain_weights", {}), dict) else {}

    sched = DeadlineScheduler(
        domains=domains,
        pools=pools,
        required_map=required_map,
        max_gap_ticks=max_gap_ticks,
        tick_ms=tick_ms,
        capacity_per_unit=capacity_per_unit,
        logs_dir=logs_dir,
        universal_roles=universal_roles,
        rotation_period_ms=120000,
        domain_weights=domain_weights,
    )

    alive = {u: True for u in units}
    initial_faults = max(0, int(initial_faults))
    faulted_units = sorted(units)[:initial_faults]
    for u in faulted_units:
        alive[u] = False

    run_summary = {
        "ticks_requested": int(ticks),
        "ticks_completed": 0,
        "initial_faults": int(initial_faults),
        "faulted_units": faulted_units,
        "tick_ms": tick_ms,
        "max_gap_ticks": max_gap_ticks,
        "capacity_per_unit": int(capacity_per_unit),
        "universal_roles": universal_roles,
    }

    try:
        for i in range(int(ticks)):
            sched.schedule_tick(alive)
            run_summary["ticks_completed"] = i + 1
    except Exception as e:
        return {"status": "FAIL", "error": str(e), "run_summary": run_summary}
    finally:
        sched.close()

    return {"status": "PASS", "error": "", "run_summary": run_summary}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mission")
    ap.add_argument("--ticks", type=int, default=200)
    ap.add_argument("--logs_dir", default="runner_logs")
    ap.add_argument("--capacity_per_unit", type=int, default=2)
    ap.add_argument("--initial_faults", type=int, default=0)
    args = ap.parse_args()

    result = run_mission(
        mission_path=args.mission,
        ticks=args.ticks,
        logs_dir=args.logs_dir,
        capacity_per_unit=args.capacity_per_unit,
        initial_faults=args.initial_faults,
    )
    print(json.dumps(result))
