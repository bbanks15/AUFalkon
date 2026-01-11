#!/usr/bin/env python3
"""hooks/validate_missions.py (drop-in)

Mission validation hook / utility.

Enhancements:
- Supports nested mission layout via recursive globs (**).
- Enforces that mission.domains includes 'rest' (reporting-only domain required by simulator).
- Aligns requirement semantics with GUI/scheduler:
  * required_active_per_domain may be int or dict
  * dict requirements default missing domains to 0
  * scalar requirements apply to all domains except 'rest'
  * 'rest' requirement is always 0

Usage:
  python hooks/validate_missions.py
  python hooks/validate_missions.py --glob "DemoProfile/*_mission.json"
  python hooks/validate_missions.py --glob "DemoProfile/*_mission.json,missions/**/mission*.json"

Exit codes:
  0 = all missions valid
  1 = no missions matched
  2 = one or more missions invalid
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


def fail(msg: str) -> None:
    raise ValueError(msg)


def load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def expand_globs(globs_csv: str) -> List[str]:
    """Expand comma-separated glob patterns into a sorted, de-duplicated file list.

    Supports ** patterns via recursive=True.
    Also supports passing a directory path (expands to <dir>/**/mission*.json).
    """
    patterns = [g.strip() for g in (globs_csv or "").split(",") if g.strip()]
    files: List[str] = []

    for pat in patterns:
        p = Path(pat)
        if p.exists() and p.is_dir():
            pat = str(p / "**" / "mission*.json")

        # Normalize separators for cross-platform runs
        pat = pat.replace("\\\\", os.sep).replace("/", os.sep)

        files.extend(glob.glob(pat, recursive=True))

    return sorted(set(files))


def normalize_required_map(m: Dict[str, Any], domains: List[str]) -> Dict[str, int]:
    req_cfg = m.get("required_active_per_domain", 1)

    def is_rest(d: str) -> bool:
        return str(d).lower() == "rest"

    required_map: Dict[str, int] = {}

    if isinstance(req_cfg, dict):
        for d in domains:
            if is_rest(d):
                required_map[d] = 0
            else:
                required_map[d] = int(req_cfg.get(d, 0))
    else:
        val = int(req_cfg)
        for d in domains:
            required_map[d] = 0 if is_rest(d) else val

    return required_map


def validate_rotation(m: Dict[str, Any]) -> None:
    rot = m.get("rotation")
    if rot is None:
        return
    if not isinstance(rot, dict):
        fail("rotation must be an object/dict if present")

    if "rest_duration_ms" in rot:
        rd = int(rot["rest_duration_ms"])
        if rd <= 0:
            fail("rotation.rest_duration_ms must be > 0")

    if "min_dwell_ms" in rot:
        md = float(rot["min_dwell_ms"])
        if md < 0:
            fail("rotation.min_dwell_ms must be >= 0")


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

    domains = m.get("domains")
    if not isinstance(domains, list) or not domains or not all(isinstance(d, str) and d for d in domains):
        fail("domains must be a non-empty list of strings")

    # Enforce REST domain for GUI/scheduler semantics
    if not any(str(d).lower() == "rest" for d in domains):
        fail("domains must include 'rest' (reporting-only domain required by simulator)")

    units = m.get("units")
    if not isinstance(units, list) or not units or not all(isinstance(u, str) and u for u in units):
        fail("units must be a non-empty list of strings")

    required_map = normalize_required_map(m, domains)
    for d, r in required_map.items():
        if r < 0:
            fail(f"required_active_per_domain for {d} must be >= 0")

    universal = bool(m.get("universal_roles", False))

    pools = m.get("domain_pools", {})
    if pools is not None and not isinstance(pools, dict):
        fail("domain_pools must be a dict/object if present")

    # If not universal, each non-rest domain with need>0 should have a non-empty pool
    if not universal:
        for d in domains:
            if str(d).lower() == "rest":
                continue
            if required_map.get(d, 0) <= 0:
                continue
            if d not in pools:
                fail(f"missing domain_pools['{d}'] (or set universal_roles=true)")
            if not isinstance(pools[d], list) or not pools[d]:
                fail(f"domain_pools['{d}'] must be a non-empty list (or set universal_roles=true)")

    unit_set = set(units)
    for k, v in (pools or {}).items():
        if isinstance(v, list):
            bad = [u for u in v if u not in unit_set]
            if bad:
                fail(f"domain_pools['{k}'] contains unknown units: {bad}")

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

    dw = m.get("domain_weights", {})
    if dw is not None:
        if not isinstance(dw, dict):
            fail("domain_weights must be an object/dict if present")
        for k, v in dw.items():
            try:
                fv = float(v)
            except Exception:
                fail(f"domain_weights['{k}'] must be numeric")
            if fv <= 0:
                fail(f"domain_weights['{k}'] must be > 0")

    validate_rotation(m)

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--glob",
        default="DemoProfile/*_mission.json,missions/**/mission*.json",
        help="Comma-separated glob(s) to mission JSON files.",
    )
    args = ap.parse_args()

    paths = expand_globs(args.glob)
    if not paths:
        print(f"No missions match: {args.glob}")
        return 1

    ok = 0
    bad = 0
    for p in paths:
        try:
            validate_one(p)
            print(f"[OK] {p}")
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
