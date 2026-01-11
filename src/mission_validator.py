"""src/mission_validator.py

Validates a mission JSON file and computes basic feasibility metrics.

Updates (aligned with GUI/scheduler semantics):
- Enforces that mission.domains includes 'rest' (reporting-only domain required by simulator).
- Normalizes required_active_per_domain into a per-domain dict:
    * If dict: missing domain keys default to 0
    * If scalar: applies to all domains except 'rest'
    * 'rest' requirement is always 0
  Requirements must be >= 0 (0 means not required).

Notes:
- This validator treats "universal_roles" missions as count-feasible if
  total capacity >= total requirements.
- It does not model domain-weighted drain or battery policies; those are runtime behaviors.
"""

from __future__ import annotations

import argparse
import json
import math
from typing import Any, Dict, List


def _required_map(mission: Dict[str, Any], domains: List[str]) -> Dict[str, int]:
    """Normalize required_active_per_domain to a per-domain dict.

    Supported:
      - int
      - dict {domain: int}

    Semantics:
      - missing keys default to 0
      - 'rest' is reporting-only => always 0
      - values must be >= 0
    """
    req_cfg = mission.get("required_active_per_domain", 1)

    def is_rest(d: str) -> bool:
        return str(d).lower() == "rest"

    rm: Dict[str, int] = {}

    if isinstance(req_cfg, dict):
        for d in domains:
            if is_rest(d):
                rm[d] = 0
            else:
                rm[d] = int(req_cfg.get(d, 0))
    else:
        val = int(req_cfg)
        for d in domains:
            rm[d] = 0 if is_rest(d) else val

    for d, v in rm.items():
        if v < 0:
            raise ValueError(f"required_active_per_domain for '{d}' must be >= 0, got {v}")

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

    # Enforce REST domain for simulator semantics
    if not any(str(d).lower() == "rest" for d in domains):
        raise ValueError("mission.domains must include 'rest' (reporting-only domain required by simulator)")

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
        needed_devices = math.ceil(needs_total / capacity_per_device) if needs_total > 0 else 0
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
