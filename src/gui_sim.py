"""src/gui_sim.py
AUFalkon Control-Layer Deadline Simulator (Tkinter).

This stitched version includes:
- _step_frame() stop-mid-frame guard to prevent closed-file writer crashes.
- HTML report embeds chart PNGs with <img> tags (no filenames-only output).
- Tooltip system intact (hoverable \u24D8 icons next to knobs).
- Mission-authoritative semantics:
  * rotation.rest_duration_ms and rotation.min_dwell_ms (converted to ticks)
  * constraints.max_gap_ms -> gap window in ticks
- REST domain is reporting-only: never required and never gap-causing.
- Report generation mid-run is a SNAPSHOT: does not interrupt sim and forces summary.json write.

Run:
  python -m src.gui_sim missions/fleet4/mission_fleet4_baseline_deadline_ms1.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
import time
import webbrowser
import html
from collections import deque
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Ensure local imports work when executed as module
HERE = os.path.dirname(__file__)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from scheduler_deadline import DeadlineScheduler  # type: ignore


LAST50_MAX = 50
SAMPLE_EVERY_TICKS = 50
REPORT_HTML = "report.html"
REPORT_LOG = "report_generation.log"
DEMO_PROFILE_DIR = "DemoProfile"

CHART_FILES = {
    "battery_heatmap": "battery_heatmap.png",
    "state_counts": "state_counts.png",
    "distinctness": "distinctness.png",
    "drain_share": "drain_share.png",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def safe_float(v: Any, default: float) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return default


def safe_int(v: Any, default: int) -> int:
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


def fmt_hms_ms(ms: int) -> str:
    ms = max(0, int(ms))
    s, rem = divmod(ms, 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{rem:03d}"


def parse_domain_weights(s: str) -> Dict[str, float]:
    txt = (s or "").strip()
    if not txt:
        return {}
    out: Dict[str, float] = {}
    for part in [p.strip() for p in txt.split(",") if p.strip()]:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        try:
            out[k] = float(v)
        except Exception:
            continue
    return out


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


# -----------------------------
# Hover tooltips for UI knobs
# -----------------------------

class _HoverTooltip:
    """Simple hover tooltip for Tkinter widgets."""

    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 400):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip = None
        self._last_xy = (0, 0)

        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<Motion>", self._on_motion, add="+")
        widget.bind("<ButtonPress>", self._on_leave, add="+")

    def _on_enter(self, event=None):
        if event is not None:
            self._last_xy = (event.x_root, event.y_root)
        self._schedule()

    def _on_motion(self, event):
        self._last_xy = (event.x_root, event.y_root)
        if self._tip is not None:
            self._move()

    def _on_leave(self, event=None):
        self._cancel()
        self._hide()

    def _schedule(self):
        self._cancel()
        try:
            self._after_id = self.widget.after(self.delay_ms, self._show)
        except Exception:
            self._after_id = None

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
        self._after_id = None

    def _show(self):
        if self._tip is not None:
            return
        try:
            self._tip = tk.Toplevel(self.widget)
            self._tip.wm_overrideredirect(True)
            self._tip.attributes("-topmost", True)

            label = tk.Label(
                self._tip,
                text=self.text,
                justify="left",
                background="#ffffe0",
                relief="solid",
                borderwidth=1,
                font=("Segoe UI", 9),
                padx=8,
                pady=6,
            )
            label.pack()
            self._move()
        except Exception:
            self._tip = None

    def _move(self):
        if self._tip is None:
            return
        x, y = self._last_xy
        x += 16
        y += 16
        try:
            self._tip.wm_geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _hide(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
        self._tip = None


def _add_info_icon(parent: tk.Widget, help_text: str) -> tk.Label:
    """Create a small hoverable info icon next to a control label."""
    icon = tk.Label(parent, text="\u24D8", fg="#0078D4", cursor="question_arrow")
    _HoverTooltip(icon, help_text)
    return icon


KNOB_HELP: Dict[str, str] = {
    "UI refresh (ms)": "How often the GUI refreshes. Smaller is smoother but uses more CPU.",
    "Sim ms / Real ms": "Simulation speed multiplier: simulated milliseconds per real millisecond.",
    "Max steps/frame": "Upper bound on simulation ticks processed per GUI refresh. Prevents UI lockup.",
    "tick_ms override": "Optional override for mission tick_ms. Leave blank to use mission tick_ms.",
    "Rotation period (ms)": "Mission-authoritative: rotation.rest_duration_ms. Controls rotation cadence.",
    "dwell ticks": "Mission-authoritative: round(rotation.min_dwell_ms / tick_ms). Minimum ticks before rotating.",
    "swap%": "Battery % threshold where swaps are preferred.",
    "reserve": "Battery reserve fraction (0-1) below which units are kept resting unless overridden.",
    "hyst": "Wake hysteresis fraction (0-1) to reduce churn.",
    "wake% (opt)": "Optional absolute wake threshold percent override.",
    "Show Last 50 ticks": "Include a compact last-50-ticks trace in the Snapshot View.",
    "Throttle low-battery warnings (30s)": "Reduce low_battery_active event spam by limiting warnings to once per unit every 30 seconds.",
    "Apply mission failure injections": "Enable mission.failure_injections during simulation.",
    "Fail immediately on gap": (
        "If enabled: show a modal once and stop the run when a required-domain gap exceeds mission max_gap_ms."
        "\nIf disabled: never stop by default (demo-friendly), only banners/logging."
    ),
    "domain_weights override": "Comma-separated domain=weight pairs (overrides mission weights).\n"
    "Example: radar_ir_gps=1.25,comm_eoir=0.9,network_test_only=0.8,rest=1.5.\n"
    "Tip: to only change one domain, supply just that pair (e.g., radar_ir_gps=1.25).",
    "Temp fail (ms)": "Temporarily marks the selected unit down for the specified duration.",
    "Gap grace (ticks)": "Mission-authoritative gap window (constraints.max_gap_ms / tick_ms). Display-only; mission wins.",
    "Generate HTML Report": "Generate an HTML report. While running, this is a SNAPSHOT and includes a fresh summary.json.",
}


class MissionGUI:
    def __init__(self, root: tk.Tk, mission_path: Optional[str] = None):
        self.root = root
        self.root.title("AUFalkon Control-Layer Deadline Simulator")

        self.base_logs_dir = "gui_logs"
        os.makedirs(self.base_logs_dir, exist_ok=True)
        os.makedirs(DEMO_PROFILE_DIR, exist_ok=True)

        self.actions_csv_path = os.path.join(self.base_logs_dir, "gui_actions.csv")
        if not os.path.exists(self.actions_csv_path):
            with open(self.actions_csv_path, "w", encoding="utf-8") as f:
                f.write("timestamp,action,detail\n")

        self.run_dir: Optional[str] = None
        self.mission: Optional[Dict[str, Any]] = None
        self.mission_path: Optional[str] = None
        self.scheduler: Optional[DeadlineScheduler] = None

        # faults
        self.temp_recover_at_ms: Dict[str, Optional[int]] = {}
        self.permanent_down: Set[str] = set()

        # gap handling
        self.gap_active = False
        self.gap_start_tick: Optional[int] = None
        self.gap_recovery_ticks = 100
        self._gap_critical_logged = False
        self._gap_failure_dialog_shown = False

        self.last50 = deque(maxlen=LAST50_MAX)

        self.running = False
        self.paused = False
        self._sim_ms_accumulator = 0.0
        self._real_start_perf: Optional[float] = None
        self._real_elapsed_before_pause_ms = 0

        self.sim_epoch = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone(timedelta(hours=-5)))

        # UI vars
        self.tick_label = tk.StringVar(value="Tick: 0")
        self.status_var = tk.StringVar(value="Ready.")
        self.alert_var = tk.StringVar(value="")
        self.sim_clock_label = tk.StringVar(value="Sim Wall-Clock: 2026-01-01 00:00:00.000")
        self.sim_elapsed_label = tk.StringVar(value="Sim Elapsed: 00:00:00.000")
        self.real_elapsed_label = tk.StringVar(value="Real Elapsed (running): 00:00:00.000")
        self.rate_label = tk.StringVar(value="Rate: 10.000×")

        # controls
        self.ui_interval_ms_var = tk.StringVar(value="50")
        self.sim_ms_per_real_ms_var = tk.StringVar(value="10")
        self.max_steps_per_frame_var = tk.StringVar(value="2000")
        self.tick_ms_override_var = tk.StringVar(value="")
        self.rotation_period_ms_var = tk.StringVar(value="120000")
        self.min_dwell_ticks_var = tk.StringVar(value="60")
        self.swap_threshold_pct_var = tk.StringVar(value="10.0")
        self.battery_reserve_pct_var = tk.StringVar(value="0.15")
        self.hysteresis_pct_var = tk.StringVar(value="0.08")
        self.wake_threshold_pct_var = tk.StringVar(value="")
        self.domain_weights_override_var = tk.StringVar(value="")

        self.show_last50_var = tk.BooleanVar(value=True)
        self.apply_failure_injections_var = tk.BooleanVar(value=False)
        self.throttle_low_battery_var = tk.BooleanVar(value=False)
        self.fail_on_gap_var = tk.BooleanVar(value=False)

        self.unit_widgets: Dict[str, Dict[str, Any]] = {}
        self.domain_rows: Dict[str, ttk.Label] = {}

        self.sel_unit_var = tk.StringVar(value="")
        self.temp_ms_var = tk.StringVar(value="10000")
        self.gap_grace_var = tk.StringVar(value=str(self.gap_recovery_ticks))

        # Layout
        ttk.Label(root, textvariable=self.status_var).pack(fill="x", padx=10, pady=(8, 0))

        clocks = ttk.Frame(root)
        clocks.pack(fill="x", padx=10, pady=(4, 0))
        ttk.Label(clocks, textvariable=self.sim_clock_label).pack(side="left")
        ttk.Label(clocks, textvariable=self.sim_elapsed_label).pack(side="left", padx=(14, 0))
        ttk.Label(clocks, textvariable=self.real_elapsed_label).pack(side="right")

        rate_row = ttk.Frame(root)
        rate_row.pack(fill="x", padx=10, pady=(2, 0))
        ttk.Label(rate_row, textvariable=self.rate_label).pack(side="left")

        self.alert_lbl = tk.Label(
            root,
            textvariable=self.alert_var,
            bg="#1f1f1f",
            fg="white",
            font=("Segoe UI", 11, "bold"),
            pady=6,
        )
        self.alert_lbl.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(6, 10))

        top = ttk.Frame(root)
        top.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(top, text="Load Mission JSON", command=self.load_mission_dialog).pack(side="left")
        ttk.Button(top, text="Start", command=self.start).pack(side="left", padx=5)
        ttk.Button(top, text="Pause/Resume", command=self.pause_resume).pack(side="left", padx=5)
        ttk.Button(top, text="Stop", command=self.stop).pack(side="left", padx=5)
        ttk.Button(top, text="Reset", command=self.reset).pack(side="left", padx=5)
        ttk.Button(top, text="Open Run Folder", command=self.open_run_folder).pack(side="left", padx=5)
        ttk.Button(top, text="Generate HTML Report", command=self.generate_html_report).pack(side="left", padx=5)
        _add_info_icon(top, KNOB_HELP["Generate HTML Report"]).pack(side="left", padx=(0, 8))
        ttk.Label(top, textvariable=self.tick_label).pack(side="right")

        ctl = ttk.LabelFrame(root, text="Controls")
        ctl.pack(fill="x", padx=10, pady=(0, 10))

        r1 = ttk.Frame(ctl)
        r1.pack(fill="x", padx=8, pady=6)
        ttk.Label(r1, text="UI refresh (ms):").pack(side="left")
        _add_info_icon(r1, KNOB_HELP["UI refresh (ms)"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r1, textvariable=self.ui_interval_ms_var, width=8).pack(side="left", padx=(6, 16))

        ttk.Label(r1, text="Sim ms / Real ms:").pack(side="left")
        _add_info_icon(r1, KNOB_HELP["Sim ms / Real ms"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r1, textvariable=self.sim_ms_per_real_ms_var, width=10).pack(side="left", padx=(6, 16))

        ttk.Label(r1, text="Max steps/frame:").pack(side="left")
        _add_info_icon(r1, KNOB_HELP["Max steps/frame"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r1, textvariable=self.max_steps_per_frame_var, width=10).pack(side="left", padx=(6, 16))

        r2 = ttk.Frame(ctl)
        r2.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(r2, text="tick_ms override:").pack(side="left")
        _add_info_icon(r2, KNOB_HELP["tick_ms override"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r2, textvariable=self.tick_ms_override_var, width=10).pack(side="left", padx=(6, 16))

        ttk.Label(r2, text="Rotation period (ms):").pack(side="left")
        _add_info_icon(r2, KNOB_HELP["Rotation period (ms)"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r2, textvariable=self.rotation_period_ms_var, width=12).pack(side="left", padx=(6, 16))

        ttk.Label(r2, text="dwell ticks:").pack(side="left")
        _add_info_icon(r2, KNOB_HELP["dwell ticks"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r2, textvariable=self.min_dwell_ticks_var, width=7).pack(side="left", padx=(6, 16))

        r3 = ttk.Frame(ctl)
        r3.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(r3, text="swap%:").pack(side="left")
        _add_info_icon(r3, KNOB_HELP["swap%"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r3, textvariable=self.swap_threshold_pct_var, width=7).pack(side="left", padx=(6, 10))

        ttk.Label(r3, text="reserve:").pack(side="left")
        _add_info_icon(r3, KNOB_HELP["reserve"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r3, textvariable=self.battery_reserve_pct_var, width=7).pack(side="left", padx=(6, 10))

        ttk.Label(r3, text="hyst:").pack(side="left")
        _add_info_icon(r3, KNOB_HELP["hyst"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r3, textvariable=self.hysteresis_pct_var, width=7).pack(side="left", padx=(6, 10))

        ttk.Label(r3, text="wake% (opt):").pack(side="left")
        _add_info_icon(r3, KNOB_HELP["wake% (opt)"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r3, textvariable=self.wake_threshold_pct_var, width=7).pack(side="left", padx=(6, 10))

        ttk.Checkbutton(r3, text="Show Last 50 ticks", variable=self.show_last50_var).pack(side="left", padx=(18, 0))
        _add_info_icon(r3, KNOB_HELP["Show Last 50 ticks"]).pack(side="left", padx=(4, 14))

        ttk.Checkbutton(
            r3, text="Throttle low-battery warnings (30s)", variable=self.throttle_low_battery_var
        ).pack(side="left", padx=(14, 0))
        _add_info_icon(r3, KNOB_HELP["Throttle low-battery warnings (30s)"]).pack(side="left", padx=(4, 14))

        ttk.Checkbutton(
            r3, text="Apply mission failure injections", variable=self.apply_failure_injections_var
        ).pack(side="left", padx=(14, 0))
        _add_info_icon(r3, KNOB_HELP["Apply mission failure injections"]).pack(side="left", padx=(4, 14))

        ttk.Checkbutton(r3, text="Fail immediately on gap", variable=self.fail_on_gap_var).pack(side="left", padx=(14, 0))
        _add_info_icon(r3, KNOB_HELP["Fail immediately on gap"]).pack(side="left", padx=(4, 14))

        r4 = ttk.Frame(ctl)
        r4.pack(fill="x", padx=8, pady=(0, 10))
        ttk.Label(r4, text="domain_weights override:").pack(side="left")
        _add_info_icon(r4, KNOB_HELP["domain_weights override"]).pack(side="left", padx=(4, 10))
        ttk.Entry(r4, textvariable=self.domain_weights_override_var, width=70).pack(side="left", padx=(6, 0))

        main = ttk.Frame(root)
        main.pack(fill="both", expand=True, padx=10, pady=10)

        self.units_frame = ttk.LabelFrame(main, text="Units")
        self.units_frame.pack(side="left", fill="y", padx=(0, 10))

        self.domains_frame = ttk.LabelFrame(main, text="Domains")
        self.domains_frame.pack(side="left", fill="both", expand=True, padx=(0, 10))

        snap = ttk.LabelFrame(main, text="Snapshot View")
        snap.pack(side="left", fill="both", expand=True)

        self.view_txt = tk.Text(snap, height=34, wrap=tk.WORD)
        self.view_txt.pack(fill="both", expand=True, padx=8, pady=8)
        self.view_txt.configure(state=tk.DISABLED)

        faults = ttk.LabelFrame(root, text="Manual Faults")
        faults.pack(fill="x", padx=10, pady=(0, 10))

        rowf = ttk.Frame(faults)
        rowf.pack(fill="x", padx=8, pady=8)

        ttk.Label(rowf, text="Selected unit:").pack(side="left")
        self.unit_combo = ttk.Combobox(rowf, textvariable=self.sel_unit_var, values=[], width=10, state="readonly")
        self.unit_combo.pack(side="left", padx=(6, 16))

        ttk.Label(rowf, text="Temp fail (ms):").pack(side="left")
        _add_info_icon(rowf, KNOB_HELP["Temp fail (ms)"]).pack(side="left", padx=(4, 10))
        ttk.Entry(rowf, textvariable=self.temp_ms_var, width=10).pack(side="left", padx=(6, 10))

        ttk.Button(rowf, text="Temporary Fail", command=self.temp_fail_selected).pack(side="left", padx=5)
        ttk.Button(rowf, text="Permanent Fail", command=self.perm_fail_selected).pack(side="left", padx=5)
        ttk.Button(rowf, text="Recover Selected", command=self.recover_selected).pack(side="left", padx=5)
        ttk.Button(rowf, text="Recover All Units", command=self.recover_all_units).pack(side="left", padx=5)

        ttk.Label(rowf, text="Gap grace (ticks):").pack(side="left", padx=(16, 0))
        _add_info_icon(rowf, KNOB_HELP["Gap grace (ticks)"]).pack(side="left", padx=(4, 10))
        ttk.Entry(rowf, textvariable=self.gap_grace_var, width=7).pack(side="left", padx=(6, 0))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if mission_path:
            self.load_mission_path(mission_path)

    # -------- actions log --------
    def _log_action(self, action: str, detail: str) -> None:
        try:
            with open(self.actions_csv_path, "a", encoding="utf-8") as f:
                f.write(f"{now_iso()},{action},{detail}\n")
        except Exception:
            pass
        if self.scheduler is not None:
            try:
                self.scheduler._emit_event("gui_action", f"{action}: {detail}")  # type: ignore
            except Exception:
                pass

    # -------- run dir --------
    def _new_run_dir(self) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        rd = os.path.join(self.base_logs_dir, f"run_{ts}")
        os.makedirs(rd, exist_ok=True)
        return rd

    def open_run_folder(self) -> None:
        if not self.run_dir or not os.path.isdir(self.run_dir):
            messagebox.showinfo("Run Folder", "No run folder yet. Start a run first.")
            return
        webbrowser.open(f"file:///{os.path.abspath(self.run_dir)}")

    # -------- mission loading --------
    def load_mission_dialog(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        self.load_mission_path(path)

    def load_mission_path(self, path: str) -> None:
        self.mission_path = path
        with open(path, "r", encoding="utf-8") as f:
            self.mission = json.load(f)

        units = self.mission.get("units", [])
        domains = self.mission.get("domains", [])
        if not units or not domains:
            messagebox.showerror("Mission error", "Mission must include non-empty 'units' and 'domains'.")
            return

        # units UI
        for w in self.units_frame.winfo_children():
            w.destroy()
        self.unit_widgets.clear()

        cols = 3
        for i, u in enumerate(units):
            cell = ttk.Frame(self.units_frame)
            cell.grid(row=i // cols, column=(i % cols), padx=4, pady=3, sticky="w")
            base_text = f"Unit {u}"
            lbl = ttk.Label(cell, text=base_text, width=14)
            lbl.pack(side="left")
            var = tk.BooleanVar(value=True)
            chk = ttk.Checkbutton(cell, text="Alive", variable=var)
            chk.pack(side="left", padx=6)
            bar = ttk.Progressbar(cell, orient="horizontal", length=120, mode="determinate", maximum=100.0)
            bar.pack(side="left", padx=6)
            bar.configure(value=100.0)
            self.unit_widgets[u] = {"label": lbl, "bar": bar, "chk": chk, "var": var, "base_text": base_text}

        # domains UI
        for w in self.domains_frame.winfo_children():
            w.destroy()
        self.domain_rows.clear()
        for d in domains:
            row = ttk.Frame(self.domains_frame)
            row.pack(fill="x", pady=2)
            lbl = ttk.Label(row, text=f"{d}: -", anchor="w")
            lbl.pack(side="left", fill="x", expand=True)
            self.domain_rows[d] = lbl

        # faults state
        self.unit_combo["values"] = list(units)
        self.sel_unit_var.set(units[0] if units else "")
        self.temp_recover_at_ms = {u: None for u in units}
        self.permanent_down = set()

        self.scheduler = None
        self.run_dir = None
        self.last50.clear()
        self._clear_alert()
        self.status_var.set(f"Loaded mission: {os.path.basename(path)}")
        self._set_snapshot("Mission loaded. Press Start.")

    # -------- scheduler init --------
    def _resolve_explicit_weights_all_domains(self) -> Dict[str, float]:
        if not self.mission:
            return {}
        domains = self.mission.get("domains", [])
        override_w = parse_domain_weights(self.domain_weights_override_var.get())
        mission_w = self.mission.get("domain_weights", {}) if isinstance(self.mission.get("domain_weights", {}), dict) else {}
        out: Dict[str, float] = {}
        for d in domains:
            if d in override_w:
                out[d] = float(override_w[d])
            elif d in mission_w:
                out[d] = safe_float(mission_w.get(d, 1.0), 1.0)
            else:
                out[d] = 1.0
        return out

    def _init_scheduler(self) -> None:
        if not self.mission:
            return

        self.run_dir = self._new_run_dir()
        self.last50.clear()
        self._clear_alert()

        domains = list(self.mission["domains"])
        units = list(self.mission["units"])

        tick_ms_mission = float(self.mission.get("tick_ms", 1.0))
        override = str(self.tick_ms_override_var.get()).strip()
        tick_ms = safe_float(override, tick_ms_mission) if override else tick_ms_mission

        max_gap_ms = int(self.mission["constraints"]["max_gap_ms"])
        max_gap_ticks = max(1, int(max_gap_ms / max(tick_ms, 0.0001)))

        # Mission-authoritative gap window
        self.gap_recovery_ticks = max_gap_ticks
        try:
            self.gap_grace_var.set(str(self.gap_recovery_ticks))
        except Exception:
            pass

        # Mission-authoritative rotation
        rotation_cfg = self.mission.get("rotation", {}) if isinstance(self.mission.get("rotation", {}), dict) else {}
        rot_period_ms_mission = safe_int(rotation_cfg.get("rest_duration_ms", 0), 0)
        min_dwell_ms_mission = safe_float(rotation_cfg.get("min_dwell_ms", 0), 0.0)

        if rot_period_ms_mission <= 0:
            rot_period_ms_mission = safe_int(self.rotation_period_ms_var.get(), 120000)

        if min_dwell_ms_mission <= 0:
            min_dwell_ticks_mission = safe_int(self.min_dwell_ticks_var.get(), 60)
        else:
            min_dwell_ticks_mission = max(0, int(round(min_dwell_ms_mission / max(tick_ms, 0.0001))))

        try:
            self.rotation_period_ms_var.set(str(rot_period_ms_mission))
        except Exception:
            pass
        try:
            self.min_dwell_ticks_var.set(str(min_dwell_ticks_mission))
        except Exception:
            pass

        required_map = self.mission.get("required_active_per_domain", {})
        if not isinstance(required_map, dict):
            # scalar -> apply to all non-rest domains; rest is always 0 required
            required_map = {d: int(required_map) for d in domains if str(d).lower() != "rest"}

        pools = {d: self.mission.get("domain_pools", {}).get(d, []) for d in domains}
        pools["spares"] = self.mission.get("domain_pools", {}).get("spares", [])

        universal = bool(self.mission.get("universal_roles", True))
        weights = self._resolve_explicit_weights_all_domains()

        self.scheduler = DeadlineScheduler(
            domains=domains,
            pools=pools,
            required_map=required_map,
            max_gap_ticks=max_gap_ticks,
            tick_ms=tick_ms,
            capacity_per_unit=2,
            logs_dir=self.run_dir,
            universal_roles=universal,
            domain_weights=weights,
            rotation_period_ms=rot_period_ms_mission,
            min_dwell_ticks=min_dwell_ticks_mission,
            swap_threshold_pct=safe_float(self.swap_threshold_pct_var.get(), 10.0),
            battery_reserve_pct=safe_float(self.battery_reserve_pct_var.get(), 0.15),
            hysteresis_pct=safe_float(self.hysteresis_pct_var.get(), 0.08),
            wake_threshold_pct=None if str(self.wake_threshold_pct_var.get()).strip() == "" else safe_float(self.wake_threshold_pct_var.get(), 0.0),
            low_battery_event_every_ms=(30000 if self.throttle_low_battery_var.get() else 0),
            low_battery_event_crossing_only=False,
            strict_mission_failure=bool(self.fail_on_gap_var.get()),
            sample_every_ticks=SAMPLE_EVERY_TICKS,
        )

        meta = {
            "created_at": now_iso(),
            "run_dir": os.path.abspath(self.run_dir),
            "mission_path": self.mission_path or "",
            "mission_file": os.path.basename(self.mission_path) if self.mission_path else "",
            "domains": domains,
            "units": units,
            "domain_weights_explicit": weights,
            "demo_profile": {
                "ui_interval_ms": safe_int(self.ui_interval_ms_var.get(), 50),
                "sim_ms_per_real_ms": safe_float(self.sim_ms_per_real_ms_var.get(), 10.0),
                "max_steps_per_frame": safe_int(self.max_steps_per_frame_var.get(), 2000),
                "tick_ms_override": str(self.tick_ms_override_var.get()),
                "rotation_period_ms": rot_period_ms_mission,
                "min_dwell_ticks": min_dwell_ticks_mission,
                "swap_threshold_pct": safe_float(self.swap_threshold_pct_var.get(), 10.0),
                "battery_reserve_pct": safe_float(self.battery_reserve_pct_var.get(), 0.15),
                "hysteresis_pct": safe_float(self.hysteresis_pct_var.get(), 0.08),
                "wake_threshold_pct": str(self.wake_threshold_pct_var.get()),
                "domain_weights_override": str(self.domain_weights_override_var.get()),
                "show_last50": bool(self.show_last50_var.get()),
                "apply_failure_injections": bool(self.apply_failure_injections_var.get()),
                "throttle_low_battery": bool(self.throttle_low_battery_var.get()),
                "fail_on_gap": bool(self.fail_on_gap_var.get()),
                "gap_recovery_ticks": self.gap_recovery_ticks,
            },
        }
        with open(os.path.join(self.run_dir, "run_meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        try:
            shutil.copyfile(self.actions_csv_path, os.path.join(self.run_dir, "gui_actions.csv"))
        except Exception:
            pass

        self.status_var.set(f"Run folder: {os.path.basename(self.run_dir)}")

    # -------- run loop --------
    def start(self) -> None:
        if not self.mission:
            messagebox.showerror("Error", "Load a mission first.")
            return
        if not self.scheduler:
            self._init_scheduler()
        self.running = True
        self.paused = False
        self._real_start_perf = time.perf_counter()
        self._sim_ms_accumulator = 0.0
        self._loop()

    def pause_resume(self) -> None:
        if not self.scheduler:
            return
        self.paused = not self.paused
        if self.paused:
            self._accumulate_real_elapsed()
            self.status_var.set("Paused.")
        else:
            self._real_start_perf = time.perf_counter()
            self.status_var.set("Running.")

    def stop(self) -> None:
        # Safe stop: prevent further ticks and close scheduler
        if self.running and not self.paused:
            self._accumulate_real_elapsed()
        self.running = False
        self.paused = False
        if self.scheduler:
            try:
                self.scheduler.close()
            except Exception:
                pass
        self.scheduler = None
        self.status_var.set("Stopped.")

    def reset(self) -> None:
        self.stop()
        self.last50.clear()
        self._set_snapshot("")
        self._clear_alert()
        for _, w in self.unit_widgets.items():
            w["chk"].configure(state="normal")
            w["var"].set(True)
            w["label"].configure(text=w["base_text"], foreground="#000000")
            w["bar"].configure(value=100.0)
        self.temp_recover_at_ms = {u: None for u in self.unit_widgets.keys()}
        self.permanent_down = set()

    def _loop(self) -> None:
        if not self.running:
            return
        if not self.paused:
            self._step_frame()
        self._update_clocks()
        self.root.after(safe_int(self.ui_interval_ms_var.get(), 50), self._loop)

    def _accumulate_real_elapsed(self) -> None:
        if self._real_start_perf is None:
            return
        self._real_elapsed_before_pause_ms += int((time.perf_counter() - self._real_start_perf) * 1000.0)
        self._real_start_perf = None

    def _compute_steps(self) -> int:
        if not self.scheduler:
            return 1
        tick_ms = max(0.0001, float(self.scheduler.tick_ms))
        ui_ms = max(1, safe_int(self.ui_interval_ms_var.get(), 50))
        rate = max(0.0, safe_float(self.sim_ms_per_real_ms_var.get(), 10.0))
        self._sim_ms_accumulator += ui_ms * rate
        steps = int(self._sim_ms_accumulator / tick_ms)
        if steps > 0:
            self._sim_ms_accumulator -= steps * tick_ms
        return max(0, min(safe_int(self.max_steps_per_frame_var.get(), 2000), steps))

    # -------- manual faults --------
    def temp_fail_selected(self) -> None:
        if not self.scheduler:
            messagebox.showinfo("Temp fail", "Start the simulation first (needs tick clock).")
            return
        u = self.sel_unit_var.get()
        if not u:
            return
        if u in getattr(self.scheduler, "battery_dead", set()):
            return
        duration = max(0, safe_int(self.temp_ms_var.get(), 10000))
        now_ms = int(self.scheduler.time_ms)
        self.temp_recover_at_ms[u] = now_ms + duration
        self.unit_widgets[u]["var"].set(False)
        self._log_action("temp_fault", f"unit={u} duration_ms={duration} now_ms={now_ms}")

    def perm_fail_selected(self) -> None:
        u = self.sel_unit_var.get()
        if not u:
            return
        self.permanent_down.add(u)
        self.unit_widgets[u]["var"].set(False)
        self._log_action("perm_fault", f"unit={u}")

    def recover_selected(self) -> None:
        u = self.sel_unit_var.get()
        if not u:
            return
        if self.scheduler and u in getattr(self.scheduler, "battery_dead", set()):
            return
        self.permanent_down.discard(u)
        self.temp_recover_at_ms[u] = None
        self.unit_widgets[u]["var"].set(True)
        self._log_action("recover_unit", f"unit={u}")

    def recover_all_units(self) -> None:
        dead = set(getattr(self.scheduler, "battery_dead", set())) if self.scheduler else set()
        for u, w in self.unit_widgets.items():
            if u in dead:
                continue
            w["var"].set(True)
        self.permanent_down.clear()
        for u in self.temp_recover_at_ms:
            self.temp_recover_at_ms[u] = None
        self._log_action("recover_all", "all non-dead units")

    # -------- alive map + injections --------
    def _alive_from_ui(self) -> Dict[str, bool]:
        return {u: bool(w["var"].get()) for u, w in self.unit_widgets.items()}

    def _apply_temp_perm_faults(self, alive: Dict[str, bool]) -> Dict[str, bool]:
        if not self.scheduler:
            return alive
        now_ms = int(self.scheduler.time_ms)
        out = dict(alive)
        for u in list(out.keys()):
            if u in self.permanent_down:
                out[u] = False
                continue
            rec = self.temp_recover_at_ms.get(u)
            if rec is not None:
                if now_ms >= int(rec):
                    self.temp_recover_at_ms[u] = None
                    if u not in self.permanent_down and u not in getattr(self.scheduler, "battery_dead", set()):
                        self.unit_widgets[u]["var"].set(True)
                    out[u] = True
                    self._log_action("temp_recovered", f"unit={u} now_ms={now_ms}")
                else:
                    out[u] = False
        return out

    def _apply_failure_injections_to_alive(self, alive: Dict[str, bool]) -> Dict[str, bool]:
        if not self.apply_failure_injections_var.get():
            return alive
        if not self.scheduler or not self.mission:
            return alive
        inj_list = self.mission.get("failure_injections") or []
        if not isinstance(inj_list, list) or not inj_list:
            return alive
        t_ms = int(self.scheduler.time_ms)
        out = dict(alive)
        for inj in inj_list:
            if not isinstance(inj, dict):
                continue
            if str(inj.get("type", "")).strip() != "unit_crash":
                continue
            unit = str(inj.get("unit", "")).strip()
            at_ms = safe_int(inj.get("at_ms", 0), 0)
            duration_ms = safe_int(inj.get("duration_ms", 0), 0)
            permanent = bool(inj.get("permanent", False))
            if not unit:
                continue
            active = False
            if t_ms >= at_ms:
                if permanent:
                    active = True
                elif duration_ms <= 0:
                    active = (t_ms == at_ms)
                else:
                    active = (t_ms < (at_ms + duration_ms))
            if active:
                out[unit] = False
        return out

    # -------- scheduler tick --------
    def _one_tick(self) -> Tuple[List[Tuple[str, str]], Dict[str, List[str]]]:
        assert self.scheduler is not None and self.mission is not None
        alive = self._alive_from_ui()
        alive = self._apply_temp_perm_faults(alive)
        alive = self._apply_failure_injections_to_alive(alive)

        try:
            assignments = self.scheduler.schedule_tick(alive)
        except Exception as e:
            msg = str(e)
            self._set_alert_critical(msg)
            self._log_action("mission_failure", msg)
            try:
                messagebox.showerror("Mission Failure", msg)
            except Exception:
                pass
            self.stop()
            return [], {d: [] for d in self.mission.get("domains", [])}

        assign_map = {d: list(self.scheduler.last_assign_map.get(d, [])) for d in self.mission.get("domains", [])}

        unmet = self._unmet_domains(assign_map)
        if unmet:
            self._handle_gap_banner(unmet)
        else:
            self._clear_gap_banner()

        rest_units = sorted(list(getattr(self.scheduler, "rest_units", set())))
        events = []
        for ev in getattr(self.scheduler, "events", []):
            try:
                events.append(asdict(ev))
            except Exception:
                events.append({"kind": getattr(ev, "kind", ""), "detail": getattr(ev, "detail", "")})

        self.last50.append(
            {"tick": int(self.scheduler.tick), "time_ms": int(self.scheduler.time_ms), "assign_map": assign_map, "rest": rest_units, "events": events}
        )

        return assignments, assign_map

    def _unmet_domains(self, assign_map: Dict[str, List[str]]) -> List[str]:
        """Return unmet domain requirements.

        - Dict requirements default to 0 when key is missing.
        - REST is reporting-only: never required and never gap-causing.
        """
        if not self.mission:
            return []
        req_cfg = self.mission.get("required_active_per_domain", 1)
        rest_dom = next((d for d in self.mission.get("domains", []) if str(d).lower() == "rest"), None)

        unmet: List[str] = []
        for d in self.mission.get("domains", []):
            if rest_dom is not None and d == rest_dom:
                continue
            if str(d).lower() == "rest":
                continue
            if isinstance(req_cfg, dict):
                need = int(req_cfg.get(d, 0))
            else:
                need = int(req_cfg)
            got = len(assign_map.get(d, []))
            if need > 0 and got < need:
                unmet.append(f"{d} need={need} got={got}")
        return unmet

    def _step_frame(self) -> None:
        """Run ticks this frame; safe if stop() closes scheduler mid-frame."""
        if not self.scheduler or not self.mission:
            return

        steps = self._compute_steps()
        last_assignments: List[Tuple[str, str]] = []
        last_assign_map: Dict[str, List[str]] = {d: [] for d in self.mission.get("domains", [])}

        for _ in range(steps):
            # --- CRITICAL FIX ---
            # If stop() was called mid-frame (e.g., fail-on-gap), do not tick again.
            if (not self.running) or self.paused or (self.scheduler is None):
                break
            last_assignments, last_assign_map = self._one_tick()
            if (not self.running) or (self.scheduler is None):
                break

        if not self.scheduler:
            return

        self.tick_label.set(f"Tick: {self.scheduler.tick}")
        self._update_domains(last_assign_map)
        self._update_units(last_assignments)
        self._update_snapshot(last_assign_map, last_assignments)

    def _update_domains(self, assign_map: Dict[str, List[str]]) -> None:
        if not self.scheduler or not self.mission:
            return
        base = float(getattr(self.scheduler, "_drain_per_role_pct", lambda: 0.0)())  # type: ignore
        weights = getattr(self.scheduler, "domain_weights", {})
        for d in self.mission.get("domains", []):
            units = assign_map.get(d, [])
            with_batt = []
            for u in units:
                b = getattr(self.scheduler, "battery_pct", {}).get(u, 0.0)
                with_batt.append(f"{u}({b:.1f}%)")
            unit_txt = ", ".join(with_batt) if with_batt else "-"
            w = float(weights.get(d, 1.0))
            drain = base * w * len(units)
            if d in self.domain_rows:
                self.domain_rows[d].configure(text=f"{d}: {unit_txt}\n drain/tick≈{drain:.6f}%")

    def _update_units(self, assignments: List[Tuple[str, str]]) -> None:
        if not self.scheduler:
            return
        active_set = {u for _, u in assignments}
        dead_set = set(getattr(self.scheduler, "battery_dead", set()))
        for u, w in self.unit_widgets.items():
            b = getattr(self.scheduler, "battery_pct", {}).get(u, 100.0)
            w["bar"].configure(value=max(0.0, min(100.0, float(b))))
            if u in dead_set:
                w["label"].configure(text=f"{w['base_text']} ☠ DEAD", foreground="#8b0000")
                w["var"].set(False)
                w["chk"].configure(state="disabled")
                continue
            w["chk"].configure(state="normal")
            if not bool(w["var"].get()):
                w["label"].configure(text=w["base_text"], foreground="#888888")
            elif u in active_set:
                w["label"].configure(text=w["base_text"], foreground="#0066cc")
            else:
                w["label"].configure(text=w["base_text"], foreground="#000000")

        alive_count = sum(1 for u, w in self.unit_widgets.items() if bool(w["var"].get()) and u not in dead_set)
        self.status_var.set(f"Available units: {alive_count}\n Active: {sorted(list(active_set))}")

    # -------- snapshot --------
    def _set_snapshot(self, text: str) -> None:
        self.view_txt.configure(state=tk.NORMAL)
        self.view_txt.delete("1.0", tk.END)
        self.view_txt.insert(tk.END, text)
        self.view_txt.configure(state=tk.DISABLED)

    def _update_snapshot(self, assign_map: Dict[str, List[str]], assignments: List[Tuple[str, str]]) -> None:
        if not self.scheduler or not self.mission:
            return
        tick = int(self.scheduler.tick)
        tms = int(self.scheduler.time_ms)

        lines: List[str] = []
        lines.append(f"Tick {tick}  time_ms={tms}\n")
        lines.append(f"max_gap_ticks={int(self.scheduler.max_gap_ticks)}  tick_ms={self.scheduler.tick_ms}\n\n")

        if self.gap_active and self.gap_start_tick is not None:
            elapsed = tick - self.gap_start_tick
            lines.append(f"GAP ACTIVE: elapsed={elapsed}/{self.gap_recovery_ticks} ticks\n\n")

        lines.append("Assignments per domain:\n")
        for d in self.mission.get("domains", []):
            units = assign_map.get(d, [])
            if units:
                parts = [f"{u}({getattr(self.scheduler, 'battery_pct', {}).get(u, 0.0):.1f}%)" for u in units]
                lines.append(f" - {d}: {', '.join(parts)}\n")
            else:
                lines.append(f" - {d}: -\n")

        if self.show_last50_var.get():
            lines.append("\nLast 50 ticks (most recent last):\n")
            for item in list(self.last50)[-LAST50_MAX:]:
                lines.append(f" t={item.get('tick')}: assign={item.get('assign_map')} rest={item.get('rest')}\n")

        self._set_snapshot("".join(lines))

    # -------- alerts --------
    def _clear_alert(self) -> None:
        self.alert_var.set("")
        self.alert_lbl.configure(bg="#1f1f1f")

    def _set_alert_warning(self, msg: str) -> None:
        self.alert_var.set(msg)
        self.alert_lbl.configure(bg="#ffcc00")

    def _set_alert_critical(self, msg: str) -> None:
        self.alert_var.set(msg)
        self.alert_lbl.configure(bg="#8b0000")

    def _handle_gap_banner(self, unmet: List[str]) -> None:
        if not self.scheduler:
            return
        tick = int(self.scheduler.tick)

        if not self.gap_active:
            self.gap_active = True
            self.gap_start_tick = tick
            self._gap_critical_logged = False
            self._gap_failure_dialog_shown = False
            self._set_alert_warning(f"Coverage gap at tick {tick}: {', '.join(unmet)}. Attempting recovery…")
            self._log_action("gap_started", "; ".join(unmet))
            return

        if self.gap_start_tick is None:
            self.gap_start_tick = tick

        elapsed = tick - self.gap_start_tick
        if elapsed < int(self.gap_recovery_ticks):
            return

        msg = f"CRITICAL: gap unresolved after {self.gap_recovery_ticks} ticks. Last: {', '.join(unmet)}"
        self._set_alert_critical(msg)

        if not self._gap_critical_logged:
            self._gap_critical_logged = True
            self._log_action("gap_critical", msg)

        if bool(self.fail_on_gap_var.get()):
            if not self._gap_failure_dialog_shown:
                self._gap_failure_dialog_shown = True
                try:
                    messagebox.showerror("Critical Failure", msg)
                except Exception:
                    pass
            self.stop()

    def _clear_gap_banner(self) -> None:
        if self.gap_active:
            self._log_action("gap_cleared", "coverage recovered")
        self.gap_active = False
        self.gap_start_tick = None
        self._gap_critical_logged = False
        self._gap_failure_dialog_shown = False
        self._clear_alert()

    # -------- clocks --------
    def _update_clocks(self) -> None:
        if not self.scheduler:
            return
        sim_ms = int(self.scheduler.time_ms)
        sim_dt = self.sim_epoch + timedelta(milliseconds=sim_ms)
        self.sim_clock_label.set(
            f"Sim Wall-Clock: {sim_dt.strftime('%Y-%m-%d %H:%M:%S')}.{int(sim_dt.microsecond/1000):03d}"
        )
        self.sim_elapsed_label.set(f"Sim Elapsed: {fmt_hms_ms(sim_ms)}")

        real_ms = int(self._real_elapsed_before_pause_ms)
        if not self.paused and self._real_start_perf is not None:
            real_ms += int((time.perf_counter() - self._real_start_perf) * 1000.0)
        self.real_elapsed_label.set(f"Real Elapsed (running): {fmt_hms_ms(real_ms)}")

        real_s = max(0.001, real_ms / 1000.0)
        sim_s = sim_ms / 1000.0
        self.rate_label.set(f"Rate: {sim_s/real_s:.3f}× (Sim: {sim_s:.3f}s / Real: {real_s:.3f}s)")

    # -------- reporting --------
    def generate_html_report(self) -> None:
        if not self.run_dir or not os.path.isdir(self.run_dir):
            messagebox.showerror("Report", "No run folder yet. Start a run first.")
            return

        # SNAPSHOT report should not interrupt the sim.
        try:
            if self.scheduler is not None and hasattr(self.scheduler, "_write_summary"):
                self.scheduler._write_summary()  # snapshot summary.json
        except Exception:
            pass

        report_type = "SNAPSHOT" if bool(self.running) else "FINAL"
        snapshot_generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

        diag = self._generate_report_pngs(self.run_dir)
        html_txt = self._render_report_html(self.run_dir, report_type=report_type, snapshot_generated_at=snapshot_generated_at)
        html_path = os.path.join(self.run_dir, REPORT_HTML)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_txt)
        messagebox.showinfo("Report Generated", diag)
        webbrowser.open(f"file:///{os.path.abspath(html_path)}")

    def _generate_report_pngs(self, run_dir: str) -> str:
        log_lines: List[str] = []
        log_lines.append(f"[{now_iso()}] report generation")
        log_lines.append(f"run_dir={os.path.abspath(run_dir)}")

        # Placeholder PNGs first (always present)
        for key, fname in CHART_FILES.items():
            try:
                placeholder_png(os.path.join(run_dir, fname), key.replace("_", " ").title(), "Generating chart…")
            except Exception as e:
                log_lines.append(f"ERROR placeholder {fname}: {e}")

        battery_csv = os.path.join(run_dir, "battery_samples.csv")
        assign_csv = os.path.join(run_dir, "assignment_samples.csv")
        events_csv = os.path.join(run_dir, "events.csv")
        summary_json = os.path.join(run_dir, "summary.json")

        log_lines.append(f"exists battery_samples.csv={os.path.exists(battery_csv)}")
        log_lines.append(f"exists assignment_samples.csv={os.path.exists(assign_csv)}")
        log_lines.append(f"exists events.csv={os.path.exists(events_csv)}")
        log_lines.append(f"exists summary.json={os.path.exists(summary_json)}")

        batt_rows: List[Dict[str, str]] = []
        if os.path.exists(battery_csv):
            try:
                with open(battery_csv, "r", encoding="utf-8") as f:
                    batt_rows = list(csv.DictReader(f))
            except Exception as e:
                log_lines.append(f"ERROR reading battery_samples.csv: {e}")

        samp_rows: List[Dict[str, str]] = []
        if os.path.exists(assign_csv):
            try:
                with open(assign_csv, "r", encoding="utf-8") as f:
                    samp_rows = list(csv.DictReader(f))
            except Exception as e:
                log_lines.append(f"ERROR reading assignment_samples.csv: {e}")

        weights: Dict[str, float] = {}
        if os.path.exists(summary_json):
            try:
                with open(summary_json, "r", encoding="utf-8") as f:
                    sj = json.load(f)
                weights = sj.get("domain_weights", {}) or {}
            except Exception:
                weights = {}

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
                    if np is not None:
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
                        placeholder_png(out_path, "Battery Heatmap", "numpy not available to render heatmap")
                        log_lines.append("NO_NUMPY battery_heatmap")
                else:
                    placeholder_png(out_path, "Battery Heatmap", "No usable battery data")
                    log_lines.append("NO_DATA battery_heatmap")
            else:
                placeholder_png(out_path, "Battery Heatmap", "No battery sample data")
                log_lines.append("NO_DATA battery_heatmap")
        except Exception as e:
            log_lines.append(f"ERROR battery_heatmap: {e}")

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
                log_lines.append("NO_DATA distinctness")
        except Exception as e:
            log_lines.append(f"ERROR distinctness: {e}")

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
                log_lines.append("NO_DATA state_counts")
        except Exception as e:
            log_lines.append(f"ERROR state_counts: {e}")

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
                log_lines.append("NO_DATA drain_share")
        except Exception as e:
            log_lines.append(f"ERROR drain_share: {e}")

        log_lines.append("--- PNG existence ---")
        for _, fname in CHART_FILES.items():
            p = os.path.join(run_dir, fname)
            log_lines.append(f"{fname} exists={os.path.exists(p)} size={os.path.getsize(p) if os.path.exists(p) else 0}")

        with open(os.path.join(run_dir, REPORT_LOG), "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines))

        diag = [f"Run folder: {os.path.abspath(run_dir)}", "", "PNG files:"]
        for _, fname in CHART_FILES.items():
            p = os.path.join(run_dir, fname)
            diag.append(f" - {fname}: {'FOUND' if os.path.exists(p) else 'MISSING'} ({os.path.getsize(p) if os.path.exists(p) else 0} bytes)")
        diag.append("")
        diag.append(f"Details: {REPORT_LOG}")
        return "\n".join(diag)

    def _render_report_html(self, run_dir: str, report_type: str = "FINAL", snapshot_generated_at: Optional[str] = None) -> str:
        meta_path = os.path.join(run_dir, "run_meta.json")
        summary_path = os.path.join(run_dir, "summary.json")
        events_path = os.path.join(run_dir, "events.csv")

        if snapshot_generated_at is None:
            snapshot_generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

        meta: Dict[str, Any] = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                meta = {}

        summary: Dict[str, Any] = {}
        if os.path.exists(summary_path):
            try:
                with open(summary_path, "r", encoding="utf-8") as f:
                    summary = json.load(f)
            except Exception:
                summary = {}

        summary_is_empty = (not isinstance(summary, dict)) or (summary == {})

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
        mission_path = meta.get("mission_path", "")

        # --- CRITICAL FIX: embed images, not filenames ---
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

        if summary_is_empty:
            summary_html = "<div class='warn'><b>Summary not available at generation time.</b></div>"
        else:
            summary_html = f"<pre>{esc(json.dumps(summary, indent=2))}</pre>"

        meta_html = f"<pre>{esc(json.dumps(meta, indent=2))}</pre>" if meta else "<div class='muted'>(no run_meta.json)</div>"

        reproduce_html = ""
        if mission_path:
            reproduce_html = (
                "<div class='card'><h3>Reproduce</h3>"
                f"<pre>python -m src.gui_sim {esc(mission_path)}\n"
                "python src/ci_gate.py --missions_glob &quot;DemoProfile/*_mission.json&quot; --ticks 200 --sweep</pre></div>"
            )

        return f"""<!doctype html>
<html>
<head>
  <meta charset='utf-8'/>
  <title>AUFalkon Demo Report</title>
  <style>{css}</style>
</head>
<body>
  <div class='hdr'>
    <h2 style='margin:0'>AUFalkon Control-Layer Deadline Simulator Report</h2>
    <div class='muted'>Run folder: <code>{esc(os.path.abspath(run_dir))}</code></div>
    <div class='muted'>Created: <code>{esc(created_at)}</code></div>
    <div class='muted'>Mission: <code>{esc(mission_file)}</code></div>
    <div style='margin-top:8px'>
      <b>Report Type:</b> {esc(report_type)}<br/>
      <b>Snapshot Generated At:</b> {esc(snapshot_generated_at)}
    </div>
    <div class='muted' style='margin-top:8px'>Images are referenced with relative paths and should render in Chrome when opened locally.</div>
  </div>

  <div class='grid'>
    <div class='card'>
      <h3>Summary</h3>
      {summary_html}
    </div>
    <div class='card'>
      <h3>Run Meta</h3>
      {meta_html}
    </div>
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

  {reproduce_html}
</body>
</html>"""

    # -------- close --------
    def _on_close(self) -> None:
        try:
            self.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mission", nargs="?", default=None, help="Optional mission JSON path to auto-load")
    args = ap.parse_args(argv)

    root = tk.Tk()
    MissionGUI(root, mission_path=args.mission)
    root.mainloop()


if __name__ == "__main__":
    main()
