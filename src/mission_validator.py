
#!/usr/bin/env python3
"""
Mission validator

- Ensures mission JSON is syntactically valid.
- Guarantees `domains` exists; derives from `required_active` if missing.
- Validates required fields and structural constraints.
- Provides clear error messages for CI gating.
"""

import argparse
import json
import sys
from typing import Dict, List


def _fail(msg: str):
    print(f"VALIDATION ERROR: {msg}", file=sys.stderr)
    raise ValueError(msg)


def validate(mission_path: str, capacity_per_device: int = 1) -> Dict:
    # --- Load JSON ---
    try:
        with open(mission_path, "r", encoding="utf-8") as f:
            mission = json.load(f)
    except json.JSONDecodeError as e:
        _fail(f"JSON parsing failed for {mission_path}: {e}")
    except FileNotFoundError:
        _fail(f"Mission file not found: {mission_path}")

    # --- Required top-level fields ---
    for key in ["name", "tick_ms", "rotation_period_ms", "required_active", "battery", "units"]:
        if key not in mission:
            _fail(f"Missing required key `{key}`")

    # --- Derive / validate domains ---
    domains = mission.get("domains")
    if not domains:
        ra = mission.get("required_active", {})
        if not isinstance(ra, dict) or len(ra) == 0:
            _fail("mission.domains must be a non-empty list (or required_active must be present to derive it)")
        domains = list(ra.keys())
        mission["domains"] = domains  # normalize in-memory for downstream use

    if not isinstance(domains, list) or len(domains) == 0:
        _fail("mission.domains must be a non-empty list")

    # --- Validate required_active ---
    required_active = mission["required_active"]
    if not isinstance(required_active, dict) or any(d not in required_active for d in domains):
        _fail("required_active must be a dict and contain an entry for each domain")
    if any(not isinstance(required_active[d], int) or required_active[d] < 0 for d in domains):
        _fail("required_active counts must be non-negative integers")

    # --- Units ---
    units = mission["units"]
    if not isinstance(units, list) or len(units) == 0:
        _fail("units must be a non-empty list")
    if any(not isinstance(u, str) or not u for u in units):
        _fail("each unit must be a non-empty string")

    # --- Battery config ---
    batt = mission["battery"]
    for key in ["max", "initial", "cost_interval_ms", "rest_recharge", "domain_cost", "cooldown"]:
        if key not in batt:
            _fail(f"battery missing `{key}`")

    if not (isinstance(batt["max"], int) and batt["max"] > 0):
        _fail("battery.max must be a positive integer")
    if not (isinstance(batt["initial"], int) and 0 <= batt["initial"] <= batt["max"]):
        _fail("battery.initial must be between 0 and battery.max")
    if not (isinstance(batt["cost_interval_ms"], int) and batt["cost_interval_ms"] > 0):
        _fail("battery.cost_interval_ms must be a positive integer")

    rr = batt["rest_recharge"]
    if not (isinstance(rr, dict) and "every_intervals" in rr and "amount" in rr):
        _fail("battery.rest_recharge must include `every_intervals` and `amount`")
    if not (isinstance(rr["every_intervals"], int) and rr["every_intervals"] > 0):
        _fail("battery.rest_recharge.every_intervals must be a positive integer")
    if not (isinstance(rr["amount"], int) and rr["amount"] >= 0):
        _fail("battery.rest_recharge.amount must be a non-negative integer")

    dc = batt["domain_cost"]
    if not (isinstance(dc, dict) and all(d in dc for d in domains)):
        _fail("battery.domain_cost must be a dict containing an integer cost for each domain")
    if any(not isinstance(dc[d], int) or dc[d] <= 0 for d in domains):
        _fail("battery.domain_cost[domain] must be positive integers")

    cd = batt["cooldown"]
    for key in ["in_ms", "out_ms"]:
        if key not in cd:
            _fail(f"battery.cooldown missing `{key}`")
    if not (isinstance(cd["in_ms"], int) and cd["in_ms"] >= 0):
        _fail("battery.cooldown.in_ms must be a non-negative integer")
    if not (isinstance(cd["out_ms"], int) and cd["out_ms"] >= 0):
        _fail("battery.cooldown.out_ms must be a non-negative integer")

    # --- Timebase checks ---
    tick_ms = mission["tick_ms"]
    rotation_period_ms = mission["rotation_period_ms"]
    if not (isinstance(tick_ms, int) and tick_ms > 0):
        _fail("tick_ms must be a positive integer")
    if not (isinstance(rotation_period_ms, int) and rotation_period_ms > 0):
        _fail("rotation_period_ms must be a positive integer")
    if rotation_period_ms % tick_ms != 0:
        _fail("rotation_period_ms must be a multiple of tick_ms")
    if batt["cost_interval_ms"] % tick_ms != 0:
        _fail("battery.cost_interval_ms must be a multiple of tick_ms")

    # --- Feasibility sanity: capacity_per_device (1 or 2) ---
    if capacity_per_device not in (1, 2):
        _fail("capacity_per_device must be 1 or 2")

    # Total roles required per tick
    total_required = sum(required_active[d] for d in domains)
    # Max roles available per tick
    max_roles = len(units) * capacity_per_device
    if total_required > max_roles:
        _fail(
            f"Infeasible: total required={total_required} exceeds max roles={max_roles} "
            f"(units={len(units)}, capacity_per_device={capacity_per_device})"
        )

    return {
        "mission_name": mission["name"],
        "domains": domains,
        "units": units,
        "total_required": total_required,
        "max_roles": max_roles,
        "capacity_per_device": capacity_per_device,
        "ok": True,
    }


def main():
    parser = argparse.ArgumentParser(description="Validate mission JSON")
    parser.add_argument("--mission", required=True, help="Path to mission JSON")
    parser.add_argument("--capacity", type=int, default=1, help="Max roles per device (1 normal, 2 contingency)")
    args = parser.parse_args()

    try:
        result = validate(args.mission, capacity_per_device=args.capacity)
        print(
            f"VALIDATION OK: {result['mission_name']} — "
            f"domains={len(result['domains'])}, units={len(result['units'])}, "
            f"required={result['total_required']}, max_roles={result['max_roles']}, "
            f"capacity={result['capacity_per_device']}"
        )
        sys.exit(0)
    except Exception as e:
        print(f"VALIDATION FAILED: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
