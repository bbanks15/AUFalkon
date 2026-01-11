#!/usr/bin/env python3
"""Synthetic check: force one unit active then resting and verify battery recharge.

Uses `DeadlineScheduler` directly (no mission_runner) to avoid changing production code.
"""
from __future__ import annotations

import time
import os, sys
REPO_ROOT = os.path.dirname(os.path.dirname(__file__))
REPO_SRC = os.path.join(REPO_ROOT, "src")
if REPO_SRC not in sys.path:
    sys.path.insert(0, REPO_SRC)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scheduler_deadline import DeadlineScheduler


def run_check():
    domains = ["radar", "rest"]
    units = ["u1", "u2"]
    required_map = {"radar": 1, "rest": 0}

    sched = DeadlineScheduler(
        domains=domains,
        pools={d: [] for d in domains},
        required_map=required_map,
        max_gap_ticks=100,
        tick_ms=1.0,
        capacity_per_unit=1,
        logs_dir=".",
        universal_roles=True,
        domain_weights={"radar": 1.0, "rest": 2.0},
        sample_every_ticks=1,
    )

    # initialize batteries
    sched._ensure_battery_initialized(units)
    sched.battery_pct["u1"] = 100.0
    sched.battery_pct["u2"] = 100.0

    # Phase 1: make only u1 available so it's active and drains
    alive = {"u1": True, "u2": False}
    ticks_active = 200
    for _ in range(ticks_active):
        sched.schedule_tick(alive)

    b_after_active = sched.battery_pct.get("u1", 0.0)
    print(f"After active phase (ticks={ticks_active}): u1 battery={b_after_active:.6f}%")

    # Phase 2: prevent u1 from being chosen for radar by adding a domain fault, so it rests
    sched.battery_pct["u2"] = 100.0
    # mark u1 as faulted for radar for the next period (non-permanent)
    sched.set_domain_fault("u1", "radar", duration_ms=100000)
    alive = {"u1": True, "u2": True}

    # Run rest period where u1 should be resting and recharge
    ticks_rest = 2000
    before_rest = sched.battery_pct.get("u1", 0.0)
    for _ in range(ticks_rest):
        sched.schedule_tick(alive)

    after_rest = sched.battery_pct.get("u1", 0.0)
    print(f"After rest phase (ticks={ticks_rest}): u1 battery={after_rest:.6f}%")

    if after_rest > before_rest + 1e-6:
        print("PASS: battery increased during rest period")
        return 0
    else:
        print("FAIL: battery did not increase during rest period")
        return 1


if __name__ == "__main__":
    raise SystemExit(run_check())
