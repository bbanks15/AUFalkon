"""src/mission_injection_audit.py

Audit mission failure injections and how they relate to mission intent.

This tool is designed to help you confirm you're "on the same path" before committing:
- Enumerates mission*.json files (recursive glob)
- Extracts scenario label, constraints, capacity pressure, and failure_injections
- Runs lightweight heuristics to flag mismatches, e.g.:
  * gap_failure scenario should have an injection that plausibly exceeds max_gap_ms (permanent or long)
  * gap_recovery scenario should have injections that resolve before max_gap_ms
  * battery_stress scenario should show high capacity pressure and/or elevated domain weights

Outputs:
- Human-readable console report
- Optional JSON summary (for CI artifacts)

Usage:
  python src/mission_injection_audit.py --glob "missions/**/mission*.json"
  python src/mission_injection_audit.py --glob "missions/**/mission*.json" --out_json audit_injections.json
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


def read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def expand(globs_csv: str) -> List[str]:
    pats = [p.strip() for p in (globs_csv or "").split(",") if p.strip()]
    out: List[str] = []
    for pat in pats:
        pat = pat.replace("\\\\", os.sep).replace("/", os.sep)
        out.extend(glob.glob(pat, recursive=True))
    return sorted(set(out))


def is_rest(d: str) -> bool:
    return str(d).lower() == "rest"


def required_map(m: Dict[str, Any]) -> Dict[str, int]:
    domains = m.get("domains", [])
    req_cfg = m.get("required_active_per_domain", 1)
    rm: Dict[str, int] = {}
    if isinstance(req_cfg, dict):
        for d in domains:
            rm[d] = 0 if is_rest(d) else int(req_cfg.get(d, 0))
    else:
        val = int(req_cfg)
        for d in domains:
            rm[d] = 0 if is_rest(d) else val
    return rm


def capacity_pressure(m: Dict[str, Any], capacity_per_unit: int = 2) -> Tuple[float, int, int]:
    units = m.get("units", [])
    n_devices = int(m.get("fleet_devices", len(units)))
    rm = required_map(m)
    need = sum(int(v) for v in rm.values())
    cap = max(0, int(n_devices) * int(capacity_per_unit))
    ratio = (need / cap) if cap > 0 else 1.0
    return ratio, need, cap


def max_domain_weight(m: Dict[str, Any]) -> float:
    dw = m.get("domain_weights", {})
    if not isinstance(dw, dict) or not dw:
        return 1.0
    mx = 1.0
    for _, v in dw.items():
        try:
            mx = max(mx, float(v))
        except Exception:
            continue
    return float(mx)


def inj_summary(m: Dict[str, Any], tick_ms: float) -> List[Dict[str, Any]]:
    inj = m.get("failure_injections", []) or []
    if not isinstance(inj, list):
        return []
    out: List[Dict[str, Any]] = []
    for ev in inj:
        if not isinstance(ev, dict):
            continue
        typ = str(ev.get("type", "")).strip()
        unit = str(ev.get("unit", "")).strip()
        at_ms = float(ev.get("at_ms", 0) or 0)
        dur_ms = float(ev.get("duration_ms", 0) or 0)
        perm = bool(ev.get("permanent", False))
        at_tick = int(round(at_ms / max(tick_ms, 1e-9)))
        dur_ticks = int(round(dur_ms / max(tick_ms, 1e-9)))
        out.append({
            "type": typ,
            "unit": unit,
            "at_ms": at_ms,
            "duration_ms": dur_ms,
            "permanent": perm,
            "at_tick": at_tick,
            "duration_ticks": dur_ticks,
        })
    return out


def classify_intent(scenario: str) -> str:
    s = (scenario or "").lower()
    if "gap" in s and "recovery" in s:
        return "gap_recovery"
    if "gap" in s and ("fail" in s or "failure" in s):
        return "gap_failure"
    if "battery" in s and ("stress" in s or "drain" in s):
        return "battery_stress"
    return "other"


def heuristic_checks(m: Dict[str, Any], capacity_per_unit: int = 2) -> List[str]:
    warnings: List[str] = []

    domains = m.get("domains", [])
    if not any(is_rest(d) for d in domains):
        warnings.append("MISSING_REST_DOMAIN")

    tick_ms = float(m.get("tick_ms", 1.0))
    max_gap_ms = int(m.get("constraints", {}).get("max_gap_ms", 0) or 0)
    intent = classify_intent(str(m.get("scenario", "")))

    inj = inj_summary(m, tick_ms)

    # Basic injection validity
    unit_set = set(m.get("units", []) or [])
    for ev in inj:
        if ev.get("unit") and ev["unit"] not in unit_set:
            warnings.append(f"INJ_UNKNOWN_UNIT:{ev['unit']}")

    # Intent-specific heuristics
    if intent == "gap_failure":
        # Expect at least one unit_crash that is permanent or duration >= max_gap_ms
        if not inj:
            warnings.append("GAP_FAILURE_WITHOUT_INJECTIONS")
        else:
            ok = False
            for ev in inj:
                if ev.get("type") != "unit_crash":
                    continue
                if bool(ev.get("permanent")):
                    ok = True
                else:
                    if max_gap_ms > 0 and float(ev.get("duration_ms", 0) or 0) >= float(max_gap_ms):
                        ok = True
            if not ok:
                warnings.append("GAP_FAILURE_INJECTIONS_DO_NOT_EXCEED_MAX_GAP")

    elif intent == "gap_recovery":
        # Expect injections that resolve before max_gap_ms (temporary and shorter)
        if not inj:
            warnings.append("GAP_RECOVERY_WITHOUT_INJECTIONS")
        else:
            bad = False
            for ev in inj:
                if ev.get("type") != "unit_crash":
                    continue
                if bool(ev.get("permanent")):
                    bad = True
                else:
                    if max_gap_ms > 0 and float(ev.get("duration_ms", 0) or 0) >= float(max_gap_ms):
                        bad = True
            if bad:
                warnings.append("GAP_RECOVERY_HAS_PERMANENT_OR_TOO_LONG_INJECTION")

    elif intent == "battery_stress":
        # Expect noticeable pressure via weights and/or requirements ratio
        ratio, need, cap = capacity_pressure(m, capacity_per_unit=capacity_per_unit)
        mxw = max_domain_weight(m)
        # heuristic thresholds: tune as needed
        if ratio < 0.75 and mxw <= 1.2:
            warnings.append(f"BATTERY_STRESS_WEAK_PRESSURE:ratio={ratio:.2f},max_weight={mxw:.2f}")

    return warnings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--glob", default="missions/**/mission*.json")
    ap.add_argument("--capacity_per_unit", type=int, default=2)
    ap.add_argument("--out_json", default="")
    args = ap.parse_args()

    paths = expand(args.glob)
    if not paths:
        print(f"No missions matched: {args.glob}")
        return 1

    results: List[Dict[str, Any]] = []

    for p in paths:
        m = read_json(p)
        tick_ms = float(m.get("tick_ms", 1.0))
        ratio, need, cap = capacity_pressure(m, capacity_per_unit=args.capacity_per_unit)
        mxw = max_domain_weight(m)

        entry = {
            "path": p,
            "scenario": m.get("scenario", ""),
            "intent": classify_intent(str(m.get("scenario", ""))),
            "tick_ms": tick_ms,
            "max_gap_ms": int(m.get("constraints", {}).get("max_gap_ms", 0) or 0),
            "mission_window_ms": m.get("mission_window_ms", None),
            "fleet_devices": int(m.get("fleet_devices", len(m.get("units", []) or []))),
            "units": len(m.get("units", []) or []),
            "domains": len(m.get("domains", []) or []),
            "required_total": int(need),
            "capacity_total": int(cap),
            "capacity_pressure": float(ratio),
            "max_domain_weight": float(mxw),
            "failure_injections": inj_summary(m, tick_ms),
            "warnings": heuristic_checks(m, capacity_per_unit=args.capacity_per_unit),
        }
        results.append(entry)

    # Print summary
    print(f"Missions checked: {len(results)}\n")
    warn_count = 0
    for r in results:
        w = r["warnings"]
        if w:
            warn_count += 1
        print(f"- {os.path.basename(r['path'])}  scenario={r['scenario']} intent={r['intent']}  injections={len(r['failure_injections'])}  pressure={r['capacity_pressure']:.2f}  maxW={r['max_domain_weight']:.2f}")
        for ww in w:
            print(f"    ! {ww}")

    print(f"\nMissions with warnings: {warn_count}/{len(results)}")

    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump({"missions": results}, f, indent=2)
        print(f"Wrote JSON: {args.out_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
