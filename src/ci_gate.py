
"""
ci_gate.py

CI gate runner that:
- Finds mission JSON files by glob
- Validates each mission via mission_validator.py
- Optionally sweeps faults from 0..Fmax using mission_runner.py
- Writes fault_sweep_summary.json
- Exits non-zero if any failures are found

Update:
- Default missions glob now points at DemoProfile exports:
    DemoProfile/*_mission.json
- Supports multiple globs via comma-separated --missions_glob
    Example:
      --missions_glob "DemoProfile/*_mission.json,missions/mission_*.json"

Parsing strategy for runner output:
- Prefer JSON
- Fall back to literal dict parsing via ast.literal_eval
"""

import sys
import json
import argparse
import glob
import os
import subprocess
import ast
from typing import Any, Dict, Tuple, List


def run_cmd(cmd) -> Tuple[int, str, str]:
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
            return json.loads(stdout_text)
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
    """
    Expand a comma-separated list of glob patterns into a sorted, de-duplicated file list.
    """
    patterns = [g.strip() for g in (globs_csv or "").split(",") if g.strip()]
    files: List[str] = []
    for pat in patterns:
        files.extend(glob.glob(pat))
    # de-dup while preserving sorted order
    return sorted(set(files))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--missions_glob",
        default="DemoProfile/*_mission.json",
        help="Comma-separated glob(s) to mission JSON files. Default: DemoProfile/*_mission.json",
    )
    ap.add_argument("--ticks", type=int, default=200)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--capacity_per_unit", type=int, default=2)
    args = ap.parse_args()

    missions = expand_globs(args.missions_glob)
    if not missions:
        print(f"No mission files found matching: {args.missions_glob}")
        return 1

    all_failures = []
    summary: Dict[str, Any] = {}

    for m in missions:
        # --- Validate mission and compute Fmax ---
        rc, out, err = run_cmd([sys.executable, "src/mission_validator.py", m, "--capacity", str(args.capacity_per_unit)])
        if rc != 0:
            all_failures.append({"mission": m, "stage": "validator_failed", "error": (err or out).strip()})
            continue

        try:
            v = json.loads(out)
        except Exception:
            all_failures.append({"mission": m, "stage": "validator_bad_json", "error": out.strip()})
            continue

        if not v.get("feasible", False):
            all_failures.append({"mission": m, "stage": "infeasible", "error": v})
            summary[m] = {"validator": v, "sweep": []}
            continue

        fmax = int(v.get("Fmax", 0))
        sweep_to = fmax if args.sweep else 0
        sweep_results = []

        base = os.path.basename(m)
        bn = os.path.splitext(base)[0]

        # --- Sweep faults from 0..Fmax (or just 0 if not sweeping) ---
        for faults in range(0, sweep_to + 1):
            logs_dir = f"runner_logs_{bn}_faults{faults}"
            rc2, out2, err2 = run_cmd([
                sys.executable, "src/mission_runner.py", m,
                "--ticks", str(args.ticks),
                "--logs_dir", logs_dir,
                "--initial_faults", str(faults),
                "--capacity_per_unit", str(args.capacity_per_unit),
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

            # Fail-fast: if any faults level fails, stop sweeping this mission
            if rj.get("status") != "PASS":
                all_failures.append({
                    "mission": m,
                    "stage": f"fault_sweep_failure_faults={faults}",
                    "error": rj.get("error", "") or (err2 or "").strip(),
                })
                break

        summary[m] = {"validator": v, "sweep": sweep_results}

    # --- Write summary artifact ---
    with open("fault_sweep_summary.json", "w", encoding="utf-8") as f:
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
