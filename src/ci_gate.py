"""src/ci_gate.py

Optimized CI gate runner (drop-in).

Fixes your nested mission layout by supporting recursive globs ("**").

What it does:
- Expands one or more mission globs (comma-separated) with recursive glob support.
- Validates each mission via src/mission_validator.py.
- Runs src/mission_runner.py for faults=0..Fmax when --sweep is set (otherwise faults=0 only).
- Writes fault_sweep_summary.json and exits non-zero on any failure.

Defaults:
- Scans missions/**/mission*.json and DemoProfile/*_mission.json

Examples:
  python src/ci_gate.py --missions_glob "missions/**/mission*.json" --ticks 200 --sweep
  python src/ci_gate.py --missions_glob "missions/*/mission*.json" --ticks 200

Notes:
- GitHub runners are case-sensitive: use 'missions', not 'Missions'.
"""

from __future__ import annotations

import argparse
import ast
import glob
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple


SUMMARY_JSON = "fault_sweep_summary.json"


def run_cmd(cmd: List[str]) -> Tuple[int, str, str]:
    """Run a subprocess and return (rc, stdout, stderr)."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    return p.returncode, out, err


def parse_runner_output(stdout_text: str, stderr_text: str, rc: int) -> Dict[str, Any]:
    """Parse runner output into a structured dict."""
    stdout_text = (stdout_text or "").strip()
    stderr_text = (stderr_text or "").strip()

    if stdout_text:
        try:
            obj = json.loads(stdout_text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    if stdout_text:
        try:
            obj = ast.literal_eval(stdout_text)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    return {
        "status": "FAIL" if rc != 0 else "UNKNOWN",
        "error": (
            f"Unparseable runner output. rc={rc}\n"
            f"STDOUT:\n{stdout_text}\n"
            f"STDERR:\n{stderr_text}"
        ),
    }


def expand_globs(globs_csv: str) -> List[str]:
    """Expand comma-separated globs into a sorted, de-duplicated list.

    Supports ** patterns via recursive=True.
    Also supports passing a directory path (expands to <dir>/**/mission*.json).
    """
    patterns = [g.strip() for g in (globs_csv or "").split(",") if g.strip()]
    files: List[str] = []

    for pat in patterns:
        p = Path(pat)
        if p.exists() and p.is_dir():
            pat = str(p / "**" / "mission*.json")

        # normalize slashes (helpful when Windows-authored YAML is executed on Linux)
        pat = pat.replace("\\\\", os.sep).replace("/", os.sep)

        files.extend(glob.glob(pat, recursive=True))

    # Normalize paths; de-dupe
    return sorted(set(str(Path(f)) for f in files))


def print_no_match_help(globs_csv: str) -> None:
    print(f"No mission files found matching: {globs_csv}")
    print("\nCommon fixes:")
    print("  - Nested per-fleet layout: missions/*/mission*.json")
    print("  - Recursive scan:          missions/**/mission*.json")
    print("  - CI is case-sensitive:    missions != Missions")

    mdir = Path("missions")
    if mdir.exists():
        sample = sorted(str(p) for p in mdir.rglob("*.json"))
        if sample:
            print("\nFound JSON under missions/ (sample):")
            for s in sample[:10]:
                print("  ", s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--missions_glob",
        default="missions/**/mission*.json,DemoProfile/*_mission.json",
        help=(
            "Comma-separated glob(s) for mission JSON files. "
            "Default: missions/**/mission*.json,DemoProfile/*_mission.json"
        ),
    )
    ap.add_argument("--ticks", type=int, default=200)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--capacity_per_unit", type=int, default=2)
    ap.add_argument("--summary_out", default=SUMMARY_JSON)
    args = ap.parse_args()

    missions = expand_globs(args.missions_glob)
    if not missions:
        print_no_match_help(args.missions_glob)
        return 1

    print(f"Found {len(missions)} mission(s).")
    for m in missions[:12]:
        print(" -", m)
    if len(missions) > 12:
        print(f" ... (+{len(missions)-12} more)")

    all_failures: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {}

    for m in missions:
        # --- Validate mission and compute Fmax ---
        rc, out, err = run_cmd([
            sys.executable,
            "src/mission_validator.py",
            m,
            "--capacity",
            str(args.capacity_per_unit),
        ])

        if rc != 0:
            all_failures.append({"mission": m, "stage": "validator_failed", "error": (err or out).strip()})
            summary[m] = {"validator": None, "sweep": []}
            continue

        try:
            v = json.loads(out)
        except Exception:
            all_failures.append({"mission": m, "stage": "validator_bad_json", "error": out.strip()})
            summary[m] = {"validator": None, "sweep": []}
            continue

        summary[m] = {"validator": v, "sweep": []}

        if not v.get("feasible", False):
            all_failures.append({"mission": m, "stage": "infeasible", "error": v})
            continue

        fmax = int(v.get("Fmax", 0))
        sweep_to = fmax if args.sweep else 0
        sweep_results: List[Dict[str, Any]] = []

        bn = os.path.splitext(os.path.basename(m))[0]

        # --- Sweep faults from 0..Fmax (or just 0 if not sweeping) ---
        for faults in range(0, sweep_to + 1):
            logs_dir = f"runner_logs_{bn}_faults{faults}"
            rc2, out2, err2 = run_cmd([
                sys.executable,
                "src/mission_runner.py",
                m,
                "--ticks",
                str(args.ticks),
                "--logs_dir",
                logs_dir,
                "--initial_faults",
                str(faults),
                "--capacity_per_unit",
                str(args.capacity_per_unit),
            ])

            rj = parse_runner_output(out2, err2, rc2)

            sweep_results.append({
                "faults": faults,
                "rc": rc2,
                "status": rj.get("status", "UNKNOWN"),
                "error": rj.get("error", ""),
                "logs_dir": logs_dir,
                "run_summary": rj.get("run_summary", {}),
                "stderr": (err2 or "").strip() if (rc2 != 0 or rj.get("status") != "PASS") else "",
            })

            # Fail-fast: stop sweeping this mission on first failure
            if rj.get("status") != "PASS":
                all_failures.append({
                    "mission": m,
                    "stage": f"fault_sweep_failure_faults={faults}",
                    "error": rj.get("error", "") or (err2 or "").strip(),
                })
                break

        summary[m]["sweep"] = sweep_results

    with open(args.summary_out, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    if all_failures:
        print("CI GATE FAILURES:")
        for x in all_failures:
            print(" -", x)
        return 2

    print("CI GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
