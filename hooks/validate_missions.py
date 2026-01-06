
#!/usr/bin/env python3
"""
Mission validation hook / utility.

Usage:
  python validate-missions.py --glob "missions/mission_*.json"

Validates:
- required top-level keys: tick_ms, domains, units, constraints.max_gap_ms
- required_active_per_domain can be int or dict per domain
- universal_roles is recommended; when true, pools can exist but are not required for feasibility
- basic sanity checks for types and positive values
"""

import argparse
import glob
import json
import sys
from typing import Any, Dict, List

def fail(msg: str) -> None:
    raise ValueError(msg)

def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def validate_one(path: str) -> Dict[str, Any]:
    m = load_json(path)

    if "tick_ms" not in m:
        fail("missing tick_ms")
    tick_ms = float(m["tick_ms"])
    if tick_ms <= 0:
        fail("tick_ms must be > 0")

    if "constraints" not in m or "max_gap_ms" not in m["constraints"]:
        fail("missing constraints.max_gap_ms")
    max_gap_ms = int(m["constraints"]["max_gap_ms"])
    if max_gap_ms <= 0:
        fail("constraints.max_gap_ms must be > 0")
    if max_gap_ms < tick_ms:
        # not fatal, but suspicious
        pass

    domains = m.get("domains")
    if not isinstance(domains, list) or not domains or not all(isinstance(d, str) and d for d in domains):
        fail("domains must be a non-empty list of strings")

    units = m.get("units")
    if not isinstance(units, list) or not units or not all(isinstance(u, str) and u for u in units):
        fail("units must be a non-empty list of strings")

    # per-domain required_active support
    req_cfg = m.get("required_active_per_domain", 1)
    required_map: Dict[str, int] = {}
    if isinstance(req_cfg, dict):
        for d in domains:
            required_map[d] = int(req_cfg.get(d, 1))
    else:
        val = int(req_cfg)
        required_map = {d: val for d in domains}

    for d, r in required_map.items():
        if r <= 0:
            fail(f"required_active_per_domain for {d} must be > 0")

    universal = bool(m.get("universal_roles", False))

    # domain_pools (optional if universal_roles=true)
    pools = m.get("domain_pools", {})
    if not isinstance(pools, dict):
        fail("domain_pools must be an object/dict if present")

    # if not universal, ensure pools exist for each domain
    if not universal:
        for d in domains:
            if d not in pools:
                fail(f"missing domain_pools entry for domain '{d}' (or set universal_roles=true)")
            if not isinstance(pools[d], list) or not pools[d]:
                fail(f"domain_pools['{d}'] must be a non-empty list (or set universal_roles=true)")

    # if pools reference units, ensure they exist
    unit_set = set(units)
    for k, v in pools.items():
        if k == "spares":
            if not isinstance(v, list):
                fail("domain_pools.spares must be a list")
            bad = [u for u in v if u not in unit_set]
            if bad:
                fail(f"domain_pools.spares contains unknown units: {bad}")
            continue
        if isinstance(v, list):
            bad = [u for u in v if u not in unit_set]
            if bad:
                fail(f"domain_pools['{k}'] contains unknown units: {bad}")

    # failure injections sanity
    inj = m.get("failure_injections", [])
    if inj is not None:
        if not isinstance(inj, list):
            fail("failure_injections must be a list if present")
        for ev in inj:
            if not isinstance(ev, dict):
                fail("each failure injection must be an object")
            if "type" not in ev or "unit" not in ev:
                fail("each failure injection must include type and unit")
            if ev["unit"] not in unit_set:
                fail(f"failure injection references unknown unit: {ev['unit']}")

    return {
        "mission": path,
        "ok": True,
        "tick_ms": tick_ms,
        "max_gap_ms": max_gap_ms,
        "domains": len(domains),
        "units": len(units),
        "universal_roles": universal,
        "required_map": required_map,
    }

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="missions/mission_*.json")
    args = ap.parse_args()

    paths = sorted(glob.glob(args.glob))
    if not paths:
        print(f"No missions match {args.glob}")
        return 1

    ok = 0
    bad = 0
    for p in paths:
        try:
            r = validate_one(p)
            print(f"[OK] {p} universal_roles={r['universal_roles']} units={r['units']} domains={r['domains']}")
            ok += 1
        except Exception as e:
            print(f"[FAIL] {p}: {e}")
            bad += 1

    if bad:
        print(f"\nValidation failed: {bad} mission(s) failed, {ok} passed.")
        return 2

    print(f"\nValidation passed: {ok} mission(s).")
    return 0

if __name__ == "__main__":
    sys.exit(main())
