
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
import time
import os
from typing import Dict, Any, List, Optional

from scheduler_deadline import DeadlineScheduler


def run_mission(
    mission_path: str,
    ticks: int,
    logs_dir: str,
    capacity_per_unit: int = 2,
    initial_faults: int = 0,
    until_failure: bool = False,
    max_real_seconds: float = 0.0,
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

    # Parse failure_injections into tick-based schedule
    failure_injections = mission.get("failure_injections", []) if isinstance(mission.get("failure_injections", []), list) else []
    injections_parsed: List[Dict[str, Optional[int]]] = []
    for inj in failure_injections:
        if not isinstance(inj, dict):
            continue
        typ = inj.get("type")
        if typ != "unit_crash":
            continue
        unit = inj.get("unit")
        at_ms = int(inj.get("at_ms", 0) or 0)
        dur = inj.get("duration_ms")
        permanent = bool(inj.get("permanent", False)) or (dur is None)
        start_tick = int(max(0, round(float(at_ms) / tick_ms)))
        end_tick: Optional[int]
        if permanent or dur is None:
            end_tick = None
        else:
            end_tick = start_tick + int(max(0, round(float(dur) / tick_ms)))
        injections_parsed.append({"unit": unit, "start_tick": start_tick, "end_tick": end_tick, "permanent": permanent})

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

    # Ensure logs_dir exists and open injection log
    try:
        os.makedirs(logs_dir, exist_ok=True)
        injection_log_f = open(os.path.join(logs_dir, "injection_log.txt"), "w", encoding="utf-8")
    except Exception:
        injection_log_f = None

    try:
        start_time = time.time()
        # track active temporary crashes: unit -> end_tick
        active_crashes: Dict[str, Optional[int]] = {}

        for i in range(int(ticks)):
            # wall-clock timeout
            if max_real_seconds and (time.time() - start_time) > float(max_real_seconds):
                return {"status": "TIMEOUT", "error": "max_real_seconds exceeded", "run_summary": run_summary}

            # expire crashes whose end_tick <= current scheduler tick (i+1)
            to_restore = []
            for u, e in list(active_crashes.items()):
                if e is not None and (i + 1) >= e:
                    to_restore.append(u)
            for u in to_restore:
                active_crashes.pop(u, None)
                # only restore if not an initial permanent fault
                if u not in run_summary.get("faulted_units", []):
                    alive[u] = True
                    if 'injection_log_f' in locals() and injection_log_f:
                        injection_log_f.write(f"tick={i+1}: RESTORE unit={u}\n")
                        injection_log_f.flush()
                    print(f"INJECTION: tick={i+1} RESTORE unit={u}")

            # apply injections starting on this tick
            for inj in injections_parsed:
                # scheduler.tick will be i+1 inside schedule_tick, so apply when start_tick == i+1
                if inj.get("start_tick") == (i + 1):
                    u = inj.get("unit")
                    if not u:
                        continue
                    # apply crash
                    alive[u] = False
                    if inj.get("permanent") or inj.get("end_tick") is None:
                        # permanent: record in run_summary.faulted_units
                        run_summary.setdefault("faulted_units", [])
                        if u not in run_summary["faulted_units"]:
                            run_summary["faulted_units"].append(u)
                    else:
                        active_crashes[u] = inj.get("end_tick")
                    if 'injection_log_f' in locals() and injection_log_f:
                        injection_log_f.write(f"tick={i+1}: APPLY crash unit={u} permanent={inj.get('permanent')} end_tick={inj.get('end_tick')}\n")
                        injection_log_f.flush()
                    # also print to stdout for CI debugging
                    print(f"INJECTION: tick={i+1} APPLY unit={u} permanent={inj.get('permanent')} end_tick={inj.get('end_tick')}")

            try:
                sched.schedule_tick(alive)
            except Exception as e:
                run_summary["ticks_completed"] = i + 1
                if until_failure:
                    return {"status": "FAIL", "error": str(e), "run_summary": run_summary}
                else:
                    # record failure but continue
                    return {"status": "FAIL", "error": str(e), "run_summary": run_summary}

            run_summary["ticks_completed"] = i + 1
    except Exception as e:
        return {"status": "FAIL", "error": str(e), "run_summary": run_summary}
    finally:
        sched.close()
        try:
            if injection_log_f:
                injection_log_f.close()
        except Exception:
            pass

    return {"status": "PASS", "error": "", "run_summary": run_summary}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mission")
    ap.add_argument("--ticks", type=int, default=200)
    ap.add_argument("--logs_dir", default="runner_logs")
    ap.add_argument("--capacity_per_unit", type=int, default=2)
    ap.add_argument("--initial_faults", type=int, default=0)
    ap.add_argument("--until_failure", action="store_true", help="Stop when scheduler raises a mission failure")
    ap.add_argument("--max_real_seconds", type=float, default=0.0, help="Wall-clock timeout in seconds (0 = disabled)")
    args = ap.parse_args()

    result = run_mission(
        mission_path=args.mission,
        ticks=args.ticks,
        logs_dir=args.logs_dir,
        capacity_per_unit=args.capacity_per_unit,
        initial_faults=args.initial_faults,
        until_failure=args.until_failure,
        max_real_seconds=args.max_real_seconds,
    )
    print(json.dumps(result))
