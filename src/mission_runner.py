
# src/mission_runner.py
from typing import Dict, List
import os

PLOTLY_AVAILABLE = False
try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    from plotly.offline import plot as plotly_offline_plot
    PLOTLY_AVAILABLE = True
except Exception:
    import matplotlib.pyplot as plt

def generate_coverage_report(coverage_records: List[Dict], domains: List[str],
                             rotation_period_ticks: int,
                             html_path: str, png_path: str):
    # Re-shape data per domain
    by_domain: Dict[str, Dict[str, List]] = {d: {"tick": [], "assigned": [], "required": [], "gap": [], "rotation": []}
                                             for d in domains}
    for rec in coverage_records:
        d = rec["domain"]
        by_domain[d]["tick"].append(rec["tick"])
        by_domain[d]["assigned"].append(rec["assigned"])
        by_domain[d]["required"].append(rec["required"])
        by_domain[d]["gap"].append(rec["gap"])
        by_domain[d]["rotation"].append(rec["rotation_boundary"])

    if PLOTLY_AVAILABLE:
        rows = len(domains)
        fig = make_subplots(rows=rows, cols=1, shared_xaxes=True,
                            subplot_titles=[f"Domain: {d}" for d in domains])

        for i, d in enumerate(domains, start=1):
            ticks = by_domain[d]["tick"]
            assigned = by_domain[d]["assigned"]
            required = by_domain[d]["required"]
            gaps = by_domain[d]["gap"]

            fig.add_trace(
                go.Scatter(x=ticks, y=assigned, name=f"{d} assigned",
                           mode="lines", line=dict(color="royalblue")),
                row=i, col=1
            )
            fig.add_trace(
                go.Scatter(x=ticks, y=required, name=f"{d} required",
                           mode="lines", line=dict(color="orange", dash="dash")),
                row=i, col=1
            )

            gap_ticks = [t for t, g in zip(ticks, gaps) if g == 1]
            gap_vals = [assigned[j] for j, g in enumerate(gaps) if g == 1]
            if gap_ticks:
                fig.add_trace(
                    go.Scatter(x=gap_ticks, y=gap_vals, name=f"{d} coverage gaps",
                               mode="markers", marker=dict(color="red", size=6, symbol="x")),
                    row=i, col=1
                )

        max_tick = max((rec["tick"] for rec in coverage_records), default=0)
        if rotation_period_ticks > 0 and max_tick > 0:
            for x in range(rotation_period_ticks, max_tick + 1, rotation_period_ticks):
                fig.add_vline(x=x, line=dict(color="gray", dash="dot", width=1))

        fig.update_layout(
            title="Coverage Timeline (Assigned vs Required) — hover to inspect",
            height=300 * len(domains),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )

        total_gaps = sum(rec["gap"] for rec in coverage_records)
        if total_gaps == 0:
            fig.add_annotation(text="✅ No coverage gaps detected",
                               xref="paper", yref="paper", x=0.01, y=1.05, showarrow=False, font=dict(color="green"))

        plotly_offline_plot(fig, filename=html_path, auto_open=False)
    else:
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
