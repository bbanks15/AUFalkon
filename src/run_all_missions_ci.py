"""src/run_all_missions_ci.py

Run all mission files and generate headless HTML reports for each.

- Finds missions by glob (recursive)
- Computes ticks to run "to completion" from mission_window_ms / tick_ms when available
  (fallback to --default_ticks)
- Runs src/mission_runner.py for each mission (initial_faults=0)
- Writes run_meta.json into each logs_dir
- Generates report.html + charts via src/report_builder.py
- Writes a top-level index.html linking all mission reports

Usage:
  python src/run_all_missions_ci.py --missions_glob "missions/**/mission*.json" --out_root ci_runs
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def _read_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def expand_globs(globs_csv: str) -> List[str]:
    patterns = [g.strip() for g in (globs_csv or "").split(",") if g.strip()]
    files: List[str] = []
    for pat in patterns:
        pat = pat.replace("\\\\", os.sep).replace("/", os.sep)
        files.extend(glob.glob(pat, recursive=True))
    return sorted(set(files))


def compute_ticks(mission: Dict[str, Any], default_ticks: int) -> int:
    # Prefer mission_window_ms if present
    mw = mission.get("mission_window_ms")
    tick_ms = float(mission.get("tick_ms", 1.0))
    if isinstance(mw, (int, float)) and mw > 0 and tick_ms > 0:
        return int(math.ceil(float(mw) / tick_ms))
    return int(default_ticks)


def run_cmd(cmd: List[str]) -> int:
    p = subprocess.Popen(cmd)
    return p.wait()


def write_meta(run_dir: str, mission_path: str, ticks: int, capacity: int) -> None:
    meta = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "run_dir": os.path.abspath(run_dir),
        "mission_path": mission_path,
        "mission_file": os.path.basename(mission_path),
        "ticks": int(ticks),
        "capacity_per_unit": int(capacity),
    }
    with open(os.path.join(run_dir, "run_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--missions_glob", default="missions/**/mission*.json")
    ap.add_argument("--out_root", default="ci_runs")
    ap.add_argument("--capacity_per_unit", type=int, default=2)
    ap.add_argument("--default_ticks", type=int, default=200)
    args = ap.parse_args()

    missions = expand_globs(args.missions_glob)
    if not missions:
        print(f"No missions matched: {args.missions_glob}")
        return 1

    out_root = args.out_root
    os.makedirs(out_root, exist_ok=True)

    report_links: List[str] = []

    for mpath in missions:
        mission = _read_json(mpath)
        ticks = compute_ticks(mission, args.default_ticks)

        bn = os.path.splitext(os.path.basename(mpath))[0]
        run_dir = os.path.join(out_root, bn)
        os.makedirs(run_dir, exist_ok=True)
        write_meta(run_dir, mpath, ticks, args.capacity_per_unit)

        # Run mission
        rc = run_cmd([
            sys.executable,
            "src/mission_runner.py",
            mpath,
            "--ticks",
            str(ticks),
            "--logs_dir",
            run_dir,
            "--capacity_per_unit",
            str(args.capacity_per_unit),
            "--initial_faults",
            "0",
        ])
        if rc != 0:
            print(f"[FAIL] mission_runner rc={rc} for {mpath}")

        # Generate report (headless)
        run_cmd([
            sys.executable,
            "src/report_builder.py",
            "--run_dir",
            run_dir,
            "--report_type",
            "FINAL",
        ])

        report_rel = f"{bn}/report.html"
        report_links.append(report_rel)
        print(f"[OK] {report_rel}")

    # Write index.html
    idx_path = os.path.join(out_root, "index.html")
    with open(idx_path, "w", encoding="utf-8") as f:
        f.write("<html><head><meta charset='utf-8'><title>AUFalkon CI Reports</title></head><body>")
        f.write("<h2>AUFalkon CI Reports</h2><ul>")
        for rel in report_links:
            f.write(f"<li><a href='{rel}'>{rel}</a></li>")
        f.write("</ul></body></html>")

    print(idx_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
