
"""
mission_validator.py

Validates a mission JSON file and computes basic feasibility metrics.

Main responsibilities:
- Validate mission structure and required keys
- Normalize required_active_per_domain into a per-domain dict
- Provide feasibility metrics given a per-device capacity assumption

Notes:
- This validator treats "universal_roles" missions as count-feasible if
  total capacity >= total requirements.
- It does not model domain-weighted drain or battery policies; those are runtime behaviors.
"""

import json
import argparse
import math
from typing import Dict, Any, List


def _required_map(mission: Dict[str, Any], domains: List[str]) -> Dict[str, int]:
    """
    Normalize required_active_per_domain to a per-domain dict.

    Supported:
      - int
      - dict {domain: int}

    Missing domain keys default to 1.
    """
    req_cfg = mission.get("required_active_per_domain", 1)
    if isinstance(req_cfg, dict):
        rm = {d: int(req_cfg.get(d, 1)) for d in domains}
    else:
        rm = {d: int(req_cfg) for d in domains}

    for d, v in rm.items():
        if v <= 0:
            raise ValueError(f"required_active_per_domain for '{d}' must be > 0, got {v}")
    return rm


def validate(mission_path: str, capacity_per_device: int = 2) -> Dict[str, Any]:
    """Validate mission JSON and compute feasibility metrics."""
    with open(mission_path, "r", encoding="utf-8") as f:
        mission = json.load(f)

    units = mission.get("units", [])
    domains = mission.get("domains", [])

    if not units or not isinstance(units, list):
        raise ValueError("mission.units must be a non-empty list")
    if not domains or not isinstance(domains, list):
        raise ValueError("mission.domains must be a non-empty list")

    n_devices = int(mission.get("fleet_devices", len(units)))
    universal = bool(mission.get("universal_roles", False))

    required_map = _required_map(mission, domains)
    needs_total = int(sum(required_map.values()))

    if capacity_per_device <= 0:
        feasible = False
        needed_devices = 10**9
        fmax = 0
    else:
        feasible = (n_devices * capacity_per_device) >= needs_total
        needed_devices = math.ceil(needs_total / capacity_per_device)
        fmax = max(0, n_devices - needed_devices)

    contingency_starts_at_faults = max(0, n_devices - needs_total + 1)

    return {
        "mission": mission_path,
        "fleet_devices": n_devices,
        "units": len(units),
        "domains": len(domains),
        "capacity_per_device": int(capacity_per_device),
        "required_active_per_domain": required_map,
        "needs_total": needs_total,
        "universal_roles": universal,
        "feasible": bool(feasible),
        "needed_devices": int(needed_devices),
        "Fmax": int(fmax),
        "contingency_starts_at_faults": int(contingency_starts_at_faults),
        "guarantee_mode": "worst_case_by_count (universal_roles assumed)" if universal else "non-universal (update validator if needed)",
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mission")
    ap.add_argument("--capacity", type=int, default=2)
    args = ap.parse_args()

    result = validate(args.mission, capacity_per_device=args.capacity)
    print(json.dumps(result, indent=2))
