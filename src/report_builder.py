"""src/report_builder.py

Headless report generator for mission_runner outputs.

Given a run directory that contains:
  - summary.json
  - events.csv
  - battery_samples.csv
  - assignment_samples.csv

This script generates:
  - battery_heatmap.png
  - state_counts.png
  - distinctness.png
  - drain_share.png
  - report_generation.log
  - report.html (embeds <img> tags)

Usage:
  python src/report_builder.py --run_dir runner_logs/my_run
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
from datetime import datetime
from typing import Any, Dict, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

CHART_FILES = {
    "battery_heatmap": "battery_heatmap.png",
    "state_counts": "state_counts.png",
    "distinctness": "distinctness.png",
    "drain_share": "drain_share.png",
}

REPORT_HTML = "report.html"
REPORT_LOG = "report_generation.log"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def write_fig_png(fig, out_path: str) -> None:
    fig.savefig(out_path, format="png", bbox_inches="tight", dpi=140)
    plt.close(fig)


def placeholder_png(out_path: str, title: str, msg: str) -> None:
    fig = plt.figure(figsize=(10, 3))
    ax = fig.add_subplot(111)
    ax.set_title(title)
    ax.axis("off")
    ax.text(0.5, 0.5, msg, ha="center", va="center", fontsize=12)
    write_fig_png(fig, out_path)


def _read_csv(path: str) -> List[Dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def generate_pngs(run_dir: str) -> str:
    log_lines: List[str] = []
    log_lines.append(f"[{now_iso()}] report generation")
    log_lines.append(f"run_dir={os.path.abspath(run_dir)}")

    battery_csv = os.path.join(run_dir, "battery_samples.csv")
    assign_csv = os.path.join(run_dir, "assignment_samples.csv")
    events_csv = os.path.join(run_dir, "events.csv")
    summary_json = os.path.join(run_dir, "summary.json")

    log_lines.append(f"exists battery_samples.csv={os.path.exists(battery_csv)}")
    log_lines.append(f"exists assignment_samples.csv={os.path.exists(assign_csv)}")
    log_lines.append(f"exists events.csv={os.path.exists(events_csv)}")
    log_lines.append(f"exists summary.json={os.path.exists(summary_json)}")

    # create placeholders first
    for k, fname in CHART_FILES.items():
        try:
            placeholder_png(os.path.join(run_dir, fname), k.replace("_", " ").title(), "Generating chart…")
        except Exception as e:
            log_lines.append(f"ERROR placeholder {fname}: {e}")

    batt_rows = _read_csv(battery_csv)
    samp_rows = _read_csv(assign_csv)
    summary = _read_json(summary_json)
    weights = summary.get("domain_weights", {}) if isinstance(summary.get("domain_weights", {}), dict) else {}

    # Battery heatmap
    try:
        out_path = os.path.join(run_dir, CHART_FILES["battery_heatmap"])
        if batt_rows:
            units = sorted({r.get("unit", "") for r in batt_rows if r.get("unit")})
            ticks = sorted({int(r.get("sample_tick", "0")) for r in batt_rows if str(r.get("sample_tick", "")).isdigit()})
            if units and ticks:
                try:
                    import numpy as np  # type: ignore
                except Exception:
                    np = None
                if np is None:
                    placeholder_png(out_path, "Battery Heatmap", "numpy not available")
                else:
                    tick_to_idx = {t: i for i, t in enumerate(ticks)}
                    u_index = {u: i for i, u in enumerate(units)}
                    mat = np.full((len(units), len(ticks)), float("nan"), dtype=float)
                    for r in batt_rows:
                        try:
                            u = r["unit"]
                            t = int(r["sample_tick"])
                            b = float(r["battery_pct"])
                            if u in u_index and t in tick_to_idx:
                                mat[u_index[u], tick_to_idx[t]] = b
                        except Exception:
                            continue
                    fig = plt.figure(figsize=(10, max(3, len(units) * 0.25)))
                    ax = fig.add_subplot(111)
                    im = ax.imshow(mat, aspect="auto", interpolation="nearest", vmin=0, vmax=100, cmap="viridis")
                    ax.set_title("Battery Heatmap (sampled)")
                    ax.set_yticks(range(len(units)))
                    ax.set_yticklabels(units)
                    ax.set_xlabel("Sample index")
                    ax.set_ylabel("Unit")
                    fig.colorbar(im, ax=ax, label="Battery %")
                    write_fig_png(fig, out_path)
                    log_lines.append("OK battery_heatmap")
            else:
                placeholder_png(out_path, "Battery Heatmap", "No usable battery data")
        else:
            placeholder_png(out_path, "Battery Heatmap", "No battery sample data")
    except Exception as e:
        log_lines.append(f"ERROR battery_heatmap: {e}")

    # State counts
    try:
        out_path = os.path.join(run_dir, CHART_FILES["state_counts"])
        if batt_rows:
            ticks = sorted({int(r.get("sample_tick", "0")) for r in batt_rows if str(r.get("sample_tick", "")).isdigit()})
            by_tick: Dict[int, List[Dict[str, str]]] = {t: [] for t in ticks}
            for r in batt_rows:
                try:
                    t = int(r.get("sample_tick", "0"))
                    if t in by_tick:
                        by_tick[t].append(r)
                except Exception:
                    continue
            active, rest, down, dead = [], [], [], []
            for t in ticks:
                c = {"active": 0, "rest": 0, "down": 0, "dead": 0}
                for rr in by_tick[t]:
                    st = rr.get("state", "")
                    if st in c:
                        c[st] += 1
                active.append(c["active"])
                rest.append(c["rest"])
                down.append(c["down"])
                dead.append(c["dead"])
            fig = plt.figure(figsize=(10, 3))
            ax = fig.add_subplot(111)
            x = list(range(len(ticks)))
            ax.plot(x, active, label="Active")
            ax.plot(x, rest, label="Rest")
            ax.plot(x, down, label="Down")
            ax.plot(x, dead, label="Dead")
            ax.set_title("Unit States Over Time (samples)")
            ax.set_xlabel("Sample index")
            ax.set_ylabel("Count")
            ax.legend(loc="upper right")
            write_fig_png(fig, out_path)
            log_lines.append("OK state_counts")
        else:
            placeholder_png(out_path, "Unit States", "No battery sample data")
    except Exception as e:
        log_lines.append(f"ERROR state_counts: {e}")

    # Distinctness
    try:
        out_path = os.path.join(run_dir, CHART_FILES["distinctness"])
        if samp_rows:
            desired = [int(float(r.get("desired_distinct", "0"))) for r in samp_rows]
            actual = [int(float(r.get("actual_distinct", "0"))) for r in samp_rows]
            fig = plt.figure(figsize=(10, 3))
            ax = fig.add_subplot(111)
            x = list(range(len(desired)))
            ax.plot(x, desired, label="Desired distinct", linewidth=2)
            ax.plot(x, actual, label="Actual distinct", linewidth=2)
            ax.fill_between(x, actual, desired, where=[a < d for a, d in zip(actual, desired)], color="red", alpha=0.15, label="Gap")
            ax.set_title("Distinctness Over Time (samples)")
            ax.set_xlabel("Sample index")
            ax.set_ylabel("Distinct devices")
            ax.legend(loc="upper right")
            write_fig_png(fig, out_path)
            log_lines.append("OK distinctness")
        else:
            placeholder_png(out_path, "Distinctness", "No assignment sample data")
    except Exception as e:
        log_lines.append(f"ERROR distinctness: {e}")

    # Drain share
    try:
        out_path = os.path.join(run_dir, CHART_FILES["drain_share"])
        if samp_rows:
            cols = [k for k in samp_rows[0].keys() if k.startswith("domain_") and k.endswith("_devices")]
            drain: Dict[str, float] = {}
            for col in cols:
                dname = col[len("domain_") : -len("_devices")]
                w = float(weights.get(dname, 1.0))
                total = 0.0
                for row in samp_rows:
                    devs = (row.get(col) or "").strip()
                    n = 0 if devs == "" else len([x for x in devs.split(";") if x.strip()])
                    total += n * w
                drain[dname] = total

            fig = plt.figure(figsize=(10, 3))
            ax = fig.add_subplot(111)
            names = list(drain.keys())
            vals = [drain[n] for n in names]
            ax.bar(names, vals, color="#4c78a8")
            ax.set_title("Domain-Weighted Drain Share (estimated)")
            ax.set_ylabel("Weighted assignment count")
            ax.tick_params(axis="x", rotation=15)
            write_fig_png(fig, out_path)
            log_lines.append("OK drain_share")
        else:
            placeholder_png(out_path, "Drain Share", "No assignment sample data")
    except Exception as e:
        log_lines.append(f"ERROR drain_share: {e}")

    log_lines.append("--- PNG existence ---")
    for _, fname in CHART_FILES.items():
        p = os.path.join(run_dir, fname)
        log_lines.append(f"{fname} exists={os.path.exists(p)} size={os.path.getsize(p) if os.path.exists(p) else 0}")

    with open(os.path.join(run_dir, REPORT_LOG), "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))

    return "\n".join(log_lines)


def render_html(run_dir: str, report_type: str = "FINAL") -> str:
    meta_path = os.path.join(run_dir, "run_meta.json")
    summary_path = os.path.join(run_dir, "summary.json")
    events_path = os.path.join(run_dir, "events.csv")

    meta = _read_json(meta_path)
    summary = _read_json(summary_path)

    events_preview: List[Dict[str, str]] = []
    if os.path.exists(events_path):
        try:
            with open(events_path, "r", encoding="utf-8") as f:
                rdr = csv.DictReader(f)
                for i, row in enumerate(rdr):
                    if i >= 60:
                        break
                    events_preview.append(row)
        except Exception:
            events_preview = []

    def esc(x: Any) -> str:
        return html.escape(str(x))

    css = """
    body { font-family: Segoe UI, Arial, sans-serif; margin: 18px; color: #111; }
    .hdr { background: #f4f6f8; padding: 12px 14px; border-radius: 10px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 14px; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 10px 12px; background: white; }
    .muted { color: #666; }
    pre { background: #0b1020; color: #e6e6e6; padding: 10px; border-radius: 10px; overflow-x: auto; }
    img { max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 8px; }
    table { border-collapse: collapse; width: 100%; }
    th, td { border: 1px solid #ddd; padding: 6px 8px; font-size: 12px; }
    th { background: #fafafa; text-align: left; }
    .warn { padding: 10px; border-radius: 10px; background: #fcf8e3; border: 1px solid #f0ad4e; color: #8a6d3b; }
    """

    created_at = meta.get("created_at", "")
    mission_file = meta.get("mission_file", "")

    summary_html = f"<pre>{esc(json.dumps(summary, indent=2))}</pre>" if summary else "<div class='warn'><b>Summary not available.</b></div>"
    meta_html = f"<pre>{esc(json.dumps(meta, indent=2))}</pre>" if meta else "<div class='muted'>(no run_meta.json)</div>"

    charts_html = "\n".join(
        f"<div class='card'><h3>{esc(k.replace('_',' ').title())}</h3>"
        f"<div class='muted'>{esc(fname)}</div>"
        f"<img src='{esc(fname)}' alt='{esc(k)}'/>"
        f"</div>"
        for k, fname in CHART_FILES.items()
    )

    events_rows = "\n".join(
        f"<tr><td>{esc(r.get('time_ticks',''))}</td><td>{esc(r.get('time_ms',''))}</td><td>{esc(r.get('kind',''))}</td><td>{esc(r.get('detail',''))}</td></tr>"
        for r in events_preview
    ) or "<tr><td colspan='4' class='muted'>(none)</td></tr>"

    gen_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    return f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'/>
  <title>AUFalkon Mission Report</title>
  <style>{css}</style>
</head>
<body>
  <div class='hdr'>
    <h2 style='margin:0'>AUFalkon Mission Report</h2>
    <div class='muted'>Run folder: <code>{esc(os.path.abspath(run_dir))}</code></div>
    <div class='muted'>Created: <code>{esc(created_at)}</code></div>
    <div class='muted'>Mission: <code>{esc(mission_file)}</code></div>
    <div style='margin-top:8px'>
      <b>Report Type:</b> {esc(report_type)}<br/>
      <b>Generated At:</b> {esc(gen_at)}
    </div>
  </div>

  <div class='grid'>
    <div class='card'><h3>Summary</h3>{summary_html}</div>
    <div class='card'><h3>Run Meta</h3>{meta_html}</div>
  </div>

  <h2 style='margin-top:18px'>Charts</h2>
  <div class='grid'>
    {charts_html}
  </div>

  <h2 style='margin-top:18px'>Recent Events (first 60)</h2>
  <div class='card'>
    <div class='muted'>Source: <code>events.csv</code></div>
    <table>
      <tr><th>tick</th><th>ms</th><th>kind</th><th>detail</th></tr>
      {events_rows}
    </table>
  </div>
</body>
</html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run_dir", required=True)
    ap.add_argument("--report_type", default="FINAL")
    args = ap.parse_args()

    run_dir = args.run_dir
    os.makedirs(run_dir, exist_ok=True)

    generate_pngs(run_dir)
    html_txt = render_html(run_dir, report_type=args.report_type)
    with open(os.path.join(run_dir, REPORT_HTML), "w", encoding="utf-8") as f:
        f.write(html_txt)
    print(os.path.join(run_dir, REPORT_HTML))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
