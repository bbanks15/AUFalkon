
# src/mission_runner.py
import csv
import os
from datetime import datetime, timedelta
from typing import Dict, List, Set

from src.scheduler_deadline import SchedulerState

# Attempt to use Plotly; fallback to static PNG with matplotlib if unavailable
PLOTLY_AVAILABLE = False
try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    from plotly.offline import plot as plotly_offline_plot
    PLOTLY_AVAILABLE = True
except Exception:
    import matplotlib.pyplot as plt

def run_mission(mission: Dict, sim_ticks: int, wall_start: datetime,
                logs_dir: str = "logs",
                write_html_coverage: bool = True):
    os.makedirs(logs_dir, exist_ok=True)
    event_stream_path = os.path.join(logs_dir, "event_stream.log")
    handoff_log_path = os.path.join(logs_dir, "handoff_log.csv")
    battery_log_path = os.path.join(logs_dir, "battery_log.csv")
    coverage_html_path = os.path.join(logs_dir, "coverage_report.html")
    coverage_png_path = os.path.join(logs_dir, "coverage_report.png")

    # Init scheduler
    S = SchedulerState(mission)
    rotation_period_ticks = mission["rotation_period_ms"] // mission["tick_ms"]
    rest_cfg = mission["battery"]["rest_recharge"]

    # Prepare logs
    with open(event_stream_path, "w", encoding="utf-8") as evt, \
         open(handoff_log_path, "w", newline="", encoding="utf-8") as hof, \
         open(battery_log_path, "w", newline="", encoding="utf-8") as bat:

        # handoff_log header: change-only events per tick
        handoff_writer = csv.writer(hof)
        handoff_writer.writerow(["tick", "sim_ms", "wall", "domain", "added_units", "removed_units", "mode"])

        # battery_log header: interval snapshots
        battery_writer = csv.writer(bat)
        battery_writer.writerow(["tick", "sim_ms", "wall", "unit", "battery", "drain_interval", "rest_interval", "mode"])

        # Coverage tracking per tick
        coverage_records: List[Dict] = []

        # Track active ticks per unit within the current cost interval
        active_ticks_map: Dict[str, int] = {u: 0 for u in mission["units"]}

        prev_assign: Dict[str, Set[str]] = {d: set() for d in S.domains}

        for tick in range(1, sim_ticks + 1):
            sim_ms = tick * mission["tick_ms"]
            wall_time = wall_start + timedelta(milliseconds=sim_ms)

            # Scheduling step
            assign = S.step(tick)

            # Event stream: rotation boundary
            if S.is_rotation_tick(tick):
                evt.write(f"[tick={tick} sim_ms={sim_ms} wall={wall_time.strftime('%m-%d-%y %H:%M:%S.%f')[:-3]}] Rotation boundary reached (2-min)\n")

            # Handoff change-only log
            for d in S.domains:
                prev = prev_assign.get(d, set())
                now = set(assign[d])
                added = sorted(list(now - prev))
                removed = sorted(list(prev - now))
                if added or removed:
                    handoff_writer.writerow([
                        tick, sim_ms, wall_time.isoformat(timespec="milliseconds"), d,
                        ";".join(added), ";".join(removed), S.mode
                    ])
                    # Narrative lines for event_stream
                    for u in removed:
                        evt.write(f"[tick={tick} sim_ms={sim_ms} wall={wall_time.strftime('%m-%d-%y %H:%M:%S.%f')[:-3]}] Device {u} rotated OUT of {d} to REST (battery={S.battery[u]})\n")
                    for u in added:
                        evt.write(f"[tick={tick} sim_ms={sim_ms} wall={wall_time.strftime('%m-%d-%y %H:%M:%S.%f')[:-3]}] Device {u} rotated IN to {d} (battery={S.battery[u]})\n")

            # Apply drain accumulation per tick and track active ticks for interval
            active_units = S.accumulate_drain(assign)
            for u in mission["units"]:
                if u in active_units:
                    active_ticks_map[u] += 1

            # Coverage record (per domain)
            for d in S.domains:
                coverage_records.append({
                    "tick": tick,
                    "domain": d,
                    "assigned": len(assign[d]),
                    "required": S.required_active[d],
                    "gap": int(len(assign[d]) < S.required_active[d]),
                    "rotation_boundary": int(S.is_rotation_tick(tick))
                })

            # Battery interval update
            if tick % S.cost_interval_ticks == 0:
                # write battery_log snapshot for the interval (all units)
                for u in mission["units"]:
                    battery_writer.writerow([
                        tick, sim_ms, wall_time.isoformat(timespec="milliseconds"), u,
                        S.battery[u],  # after applying drain below we will update, so write pre-update accum? We'll include drain from accum map
                        # For audit clarity, we store total drain for the interval via last accum before reset:
                        # (We already incremented drain per tick; at interval end, it's applied and reset)
                        # To show the exact drain that was applied, compute from active_ticks_map * domain_cost at unit-level is complex.
                        # We'll log current drain_accum (applied below) before reset.
                        # capture planned drain:
                        S.drain_accum[u],
                        S.rest_intervals[u],
                        S.mode
                    ])

                # Apply drain + recovery at interval boundary
                S.update_battery_interval(active_ticks_map, rest_cfg)

                # Reset active tick counters for next interval
                active_ticks_map = {u: 0 for u in mission["units"]}

                # Narrative battery lines
                evt.write(f"[tick={tick} sim_ms={sim_ms} wall={wall_time.strftime('%m-%d-%y %H:%M:%S.%f')[:-3]}] Battery interval applied; recovery evaluated for REST\n")

            # Remember previous assignment
            prev_assign = {d: set(assign[d]) for d in S.domains}

        # After run: generate interactive coverage report
        if write_html_coverage:
            generate_coverage_report(coverage_records, S.domains, rotation_period_ticks,
                                     coverage_html_path, coverage_png_path)

    return {
        "event_stream.log": event_stream_path,
        "handoff_log.csv": handoff_log_path,
        "battery_log.csv": battery_log_path,
        "coverage_report.html": coverage_html_path
    }

def generate_coverage_report(coverage_records: List[Dict], domains: List[str],
                             rotation_period_ticks: int,
                             html_path: str, png_path: str):
    # Re-shape data per domain
    by_domain: Dict[str, Dict[str, List]] = {d: {"tick": [], "assigned": [], "required": [], "gap": [], "rotation": []}
                                             for d in domains}
    for rec in coverage_records:
        d = rec["domain"]
        for k in ["tick", "assigned", "required", "gap", "rotation_boundary"]:
            key = "rotation" if k == "rotation_boundary" else k
            by_domain[d][key].append(rec[k])

    if PLOTLY_AVAILABLE:
        # Interactive figure with one row per domain
        rows = len(domains)
        fig = make_subplots(rows=rows, cols=1, shared_xaxes=True,
                            subplot_titles=[f"Domain: {d}" for d in domains])

        for i, d in enumerate(domains, start=1):
            ticks = by_domain[d]["tick"]
            assigned = by_domain[d]["assigned"]
            required = by_domain[d]["required"]
            gaps = by_domain[d]["gap"]

            fig.add_trace(go.Scatter(
                x=ticks, y=assigned, name=f"{d} assigned",
                mode="lines", line=dict(color="royalblue")), row=i, col=1)

            fig.add_trace(go.Scatter(
                x=ticks, y=required, name=f"{d} required",
                mode="lines", line=dict(color="orange", dash="dash")), row=i, col=1)

            # Highlight gaps as red markers
            gap_ticks = [t for t, g in zip(ticks, gaps) if g == 1]
            gap_vals = [assigned[j] for j, g in enumerate(gaps) if g == 1]
            if gap_ticks:
                fig.add_trace(go.Scatter(
                    x=gap_ticks, y=gap_vals,
                    name=f"{d} coverage gaps",
                    mode="markers", marker=dict(color="red", size=6, symbol="x")),
                    row=i, col=1)

        # Add vertical lines for rotation boundaries
        max_tick = max(coverage_records, key=lambda r: r["tick"])["tick"] if coverage_records else 0
        if rotation_period_ticks > 0 and max_tick > 0:
            for x in range(rotation_period_ticks, max_tick + 1, rotation_period_ticks):
                fig.add_vline(x=x, line=dict(color="gray", dash="dot", width=1))

        fig.update_layout(
            title="Coverage Timeline (Assigned vs Required) — hover to inspect",
            height=300 * len(domains),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )

        # Annotate if there are no gaps at all
        total_gaps = sum(rec["gap"] for rec in coverage_records)
        if total_gaps == 0:
            fig.add_annotation(text="✅ No coverage gaps detected",
                               xref="paper", yref="paper", x=0.01, y=1.05, showarrow=False, font=dict(color="green"))

        plotly_offline_plot(fig, filename=html_path, auto_open=False)
    else:
        # Fallback static PNG (matplotlib)
        rows = len(domains)
        fig, axes = plt.subplots(rows, 1, figsize=(12, 3 * rows), sharex=True)
        if rows == 1:
            axes = [axes]
        for ax, d in zip(axes, domains):
            ticks = by_domain[d]["tick"]
            assigned = by_domain[d]["assigned"]
            required = by_domain[d]["required"]
            ax.plot(ticks, assigned, label=f"{d} assigned", color="royalblue")
            ax.plot(ticks, required, label=f"{d} required", color="orange", linestyle="--")
            gaps = by_domain[d]["gap"]
            gap_ticks = [t for t, g in zip(ticks, gaps) if g == 1]
            gap_vals = [assigned[j] for j, g in enumerate(gaps) if g == 1]
            ax.scatter(gap_ticks, gap_vals, label=f"{d} gaps", color="red", marker="x")
            ax.legend()
            ax.set_ylabel("Units")
        axes[-1].set_xlabel("Tick")
        fig.suptitle("Coverage Timeline (Assigned vs Required)")
        fig.tight_layout()
        fig.savefig(png_path)
