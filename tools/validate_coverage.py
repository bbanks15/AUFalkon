
#!/usr/bin/env python3
"""
CI coverage validator — fails the workflow if any coverage gaps were detected.

It expects a JSON summary per mission at:
  logs/<mission_name>/coverage_summary.json

Each JSON file should look like:
{
  "total_gap_ticks": <int>,
  "gaps_by_domain": { "<domain>": <int>, ... }
}

Exit codes:
  0 — all missions have zero gap ticks
  1 — one or more missions missing/invalid summary, or any mission has gap ticks > 0
"""

import os
import sys
import json

def validate_summary(summary_path: str) -> int:
    """
    Returns the number of gap ticks from the given summary path.
    Raises on missing or invalid files to enforce CI strictness.
    """
    if not os.path.exists(summary_path):
        raise FileNotFoundError(f"summary not found: {summary_path}")

    with open(summary_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    total_gaps = data.get("total_gap_ticks")
    if total_gaps is None or not isinstance(total_gaps, int):
        raise ValueError(f"invalid summary format (missing integer total_gap_ticks): {summary_path}")

    return total_gaps

def main() -> None:
    base_logs = "logs"
    if not os.path.isdir(base_logs):
        print(f"❌ Logs directory not found: {base_logs}", file=sys.stderr)
        sys.exit(1)

    failures = []
    checked = 0

    # Iterate over mission subdirectories, e.g., logs/mission_fleet12_deadline_ms1/
    for entry in os.listdir(base_logs):
        mission_dir = os.path.join(base_logs, entry)
        if not os.path.isdir(mission_dir):
            continue

        summary_path = os.path.join(mission_dir, "coverage_summary.json")
        try:
            gaps = validate_summary(summary_path)
            checked += 1
            if gaps > 0:
                failures.append((entry, gaps))
        except Exception as e:
            failures.append((entry, f"missing/invalid: {e}"))

    if checked == 0:
        print("❌ No mission coverage summaries found under logs/", file=sys.stderr)
        sys.exit(1)

    if failures:
        print("❌ Coverage validation failed:")
        for mission_name, info in failures:
            print(f"  - {mission_name}: {info}")
        sys.exit(1)

    print("✅ Coverage validation passed for all missions (no gaps).")
    sys.exit(0)

if __name__ == "__main__":
    main()
