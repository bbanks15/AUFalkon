
#!/usr/bin/env python3
"""
AUFalkon Tkinter GUI Simulator

- Live window showing:
  - Tick, mode, and rotation countdown
  - Per-domain assignments (live)
  - Battery bars (0-100%) with color-coded cooldown indicators (IN=green, OUT=red)
- Uses SchedulerState and logging similar to mission_runner
- Generates interactive coverage_report.html at the end
"""

import argparse
import os
import tkinter as tk
from tkinter import ttk
from datetime import datetime, timedelta
from typing import Dict, List, Set

from src.scheduler_deadline import SchedulerState
from src.mission_runner import generate_coverage_report

# ---------- Helpers ----------
def load_mission(path: str) -> dict:
    import json
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def pct(val: int, maximum: int) -> int:
    return int((val / maximum) * 100) if maximum > 0 else 0


class TkSimApp(tk.Tk):
    def __init__(self, mission_path: str, sim_ticks: int, step_per_frame: int = 1000):
        super().__init__()
        self.title("AUFalkon — Mission Simulator")

        # Mission and scheduler
        self.mission_path = mission_path
        self.mission = load_mission(mission_path)
        self.S = SchedulerState(self.mission)
        self.tick_ms = self.mission.get("tick_ms", 1)
        self.rotation_period_ticks = self.mission["rotation_period_ms"] // self.tick_ms
        self.rest_cfg = self.mission["battery"]["rest_recharge"]

        self.sim_ticks_target = sim_ticks
        self.step_per_frame = step_per_frame
        self.current_tick = 0
        self.running = True

        # Coverage + interval tracking
        self.coverage_records: List[Dict] = []
        self.prev_assign: Dict[str, Set[str]] = {d: set() for d in self.S.domains}
        self.active_ticks_map: Dict[str, int] = {u: 0 for u in self.mission["units"]}

        # Logs dir
        self.mname = os.path.splitext(os.path.basename(mission_path))[0]
        self.logs_dir = os.path.join("logs", self.mname)
        os.makedirs(self.logs_dir, exist_ok=True)

        # File paths
        self.event_stream_path = os.path.join(self.logs_dir, "event_stream.log")
        self.handoff_log_path = os.path.join(self.logs_dir, "handoff_log.csv")
        self.battery_log_path = os.path.join(self.logs_dir, "battery_log.csv")
        self.coverage_html_path = os.path.join(self.logs_dir, "coverage_report.html")
        self.coverage_png_path = os.path.join(self.logs_dir, "coverage_report.png")

        # Open logs
        import csv
        self.evt = open(self.event_stream_path, "w", encoding="utf-8")
        self.hof = open(self.handoff_log_path, "w", newline="", encoding="utf-8")
        self.bat = open(self.battery_log_path, "w", newline="", encoding="utf-8")
        self.handoff_writer = csv.writer(self.hof)
        self.handoff_writer.writerow(["tick", "sim_ms", "wall", "domain", "added_units", "removed_units", "mode"])
        self.battery_writer = csv.writer(self.bat)
        self.battery_writer.writerow(["tick", "sim_ms", "wall", "unit", "battery", "drain_applied", "recovery_applied", "rest_interval", "mode"])

        self.wall_start = datetime.utcnow()

        # ---------- UI ----------
        self._build_ui()

        # Begin loop
        self.after(10, self._frame_step)

    def _build_ui(self):
        # Header
        header = ttk.Frame(self)
        header.pack(fill="x", padx=10, pady=8)

        ttk.Label(header, text=f"Mission: {self.mname}", font=("Segoe UI", 12, "bold")).pack(side="left")
        self.tick_label = ttk.Label(header, text="Tick: 0")
        self.tick_label.pack(side="left", padx=10)
        self.mode_label = ttk.Label(header, text="Mode: NORMAL")
        self.mode_label.pack(side="left", padx=10)
        self.rotation_label = ttk.Label(header, text="Next rotation in …")
        self.rotation_label.pack(side="left", padx=10)

        # Main panes
        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=10, pady=8)

        # Left: Assignments per domain
        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        ttk.Label(left, text="Assignments", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.domain_frames: Dict[str, ttk.Frame] = {}

        for d in self.S.domains:
            df = ttk.Frame(left, relief="groove", borderwidth=1)
            df.pack(fill="x", pady=4)
            ttk.Label(df, text=f"{d} (required: {self.S.required_active[d]})", font=("Segoe UI", 9, "bold")).pack(anchor="w")
            lb = tk.Listbox(df, height=4)
            lb.pack(fill="x", padx=4, pady=2)
            self.domain_frames[d] = lb

        # Right: Battery bars grid
        right = ttk.Frame(panes)
        panes.add(right, weight=1)
        ttk.Label(right, text="Battery / Cooldowns", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0,6))

        self.batt_rows: Dict[str, Dict] = {}
        for i, u in enumerate(sorted(self.mission["units"]), start=1):
            ttk.Label(right, text=u, width=4).grid(row=i, column=0, sticky="w")
            pb = ttk.Progressbar(right, orient="horizontal", length=200, mode="determinate", maximum=100)  # percent
            pb.grid(row=i, column=1, sticky="we", padx=4)
            val_lbl = ttk.Label(right, text="0% (0/100000)")
            val_lbl.grid(row=i, column=2, sticky="w")
            cd_lbl = ttk.Label(right, text="", foreground="gray")
            cd_lbl.grid(row=i, column=3, sticky="w", padx=4)
            self.batt_rows[u] = {"pb": pb, "val": val_lbl, "cd": cd_lbl}

        # Controls
        controls = ttk.Frame(self)
        controls.pack(fill="x", padx=10, pady=8)
        self.start_btn = ttk.Button(controls, text="Pause", command=self._toggle_run)
        self.start_btn.pack(side="left")
        ttk.Button(controls, text="Stop & Save Report", command=self._finalize_and_exit).pack(side="left", padx=8)

        ttk.Label(controls, text=f"Logs: {self.logs_dir}").pack(side="right")

    def _toggle_run(self):
        self.running = not self.running
        self.start_btn.config(text=("Resume" if not self.running else "Pause"))
        if self.running:
            self.after(10, self._frame_step)

    def _frame_step(self):
        if not self.running:
            return
        # Simulate up to step_per_frame ticks per frame to keep UI responsive
        steps = min(self.step_per_frame, self.sim_ticks_target - self.current_tick)
        for _ in range(steps):
            self.current_tick += 1
            self._tick_once(self.current_tick)
            if self.current_tick >= self.sim_ticks_target:
                break

        self._refresh_ui()

        if self.current_tick < self.sim_ticks_target:
            self.after(10, self._frame_step)
        else:
            self._finalize_and_exit()

    def _tick_once(self, tick: int):
        sim_ms = tick * self.tick_ms
        wall_time = self.wall_start + timedelta(milliseconds=sim_ms)
        assign = self.S.step(tick)

        # Rotation narrative
        if self.S.is_rotation_tick(tick):
            self.evt.write(f"[tick={tick} sim_ms={sim_ms} wall={wall_time.strftime('%m-%d-%y %H:%M:%S.%f')[:-3]}] Rotation boundary reached (2-min)\n")

        # Handoffs change-only + narrative
        for d in self.S.domains:
            prev = self.prev_assign.get(d, set())
            now = set(assign[d])
            added = sorted(list(now - prev))
            removed = sorted(list(prev - now))
            if added or removed:
                self.handoff_writer.writerow([
                    tick, sim_ms, wall_time.isoformat(timespec="milliseconds"), d,
                    ";".join(added), ";".join(removed), self.S.mode
                ])
                for u in removed:
                    self.evt.write(f"[tick={tick} sim_ms={sim_ms} wall={wall_time.strftime('%m-%d-%y %H:%M:%S.%f')[:-3]}] Device {u} rotated OUT of {d} to REST (battery={self.S.battery[u]})\n")
                for u in added:
                    self.evt.write(f"[tick={tick} sim_ms={sim_ms} wall={wall_time.strftime('%m-%d-%y %H:%M:%S.%f')[:-3]}] Device {u} rotated IN to {d} (battery={self.S.battery[u]})\n")

        # Accumulate drain
        active_units = self.S.accumulate_drain(assign)
        for u in self.mission["units"]:
            if u in active_units:
                self.active_ticks_map[u] += 1

        # Coverage record
        for d in self.S.domains:
            self.coverage_records.append({
                "tick": tick,
                "domain": d,
                "assigned": len(assign[d]),
                "required": self.S.required_active[d],
                "gap": int(len(assign[d]) < self.S.required_active[d]),
                "rotation_boundary": int(self.S.is_rotation_tick(tick))
            })

        # Interval boundary
        if tick % self.S.cost_interval_ticks == 0:
            interval_drain = {u: self.S.drain_accum[u] for u in self.mission["units"]}
            pre_battery = {u: self.S.battery[u] for u in self.mission["units"]}

            self.S.update_battery_interval(self.active_ticks_map, self.rest_cfg)

            post_battery = {u: self.S.battery[u] for u in self.mission["units"]}
            interval_recovery = {u: max(0, post_battery[u] - max(0, pre_battery[u] - interval_drain[u]))
                                 for u in self.mission["units"]}

            for u in self.mission["units"]:
                self.battery_writer.writerow([
                    tick, sim_ms, wall_time.isoformat(timespec="milliseconds"), u,
                    post_battery[u], interval_drain[u], interval_recovery[u],
                    self.S.rest_intervals[u], self.S.mode
                ])

            self.active_ticks_map = {u: 0 for u in self.mission["units"]}
            self.evt.write(f"[tick={tick} sim_ms={sim_ms} wall={wall_time.strftime('%m-%d-%y %H:%M:%S.%f')[:-3]}] Battery interval applied; recovery evaluated for REST\n")

        # Remember previous assignment
        self.prev_assign = {d: set(assign[d]) for d in self.S.domains}

        # Update domain listboxes live
        for d in self.S.domains:
            lb = self.domain_frames[d]
            lb.delete(0, tk.END)
            for u in assign[d]:
                lb.insert(tk.END, u)

    def _refresh_ui(self):
        # Header labels
        self.tick_label.config(text=f"Tick: {self.current_tick}")
        self.mode_label.config(text=f"Mode: {self.S.mode}")
        remaining = self.rotation_period_ticks - (self.current_tick % self.rotation_period_ticks)
        if remaining == self.rotation_period_ticks:
            remaining = 0
        self.rotation_label.config(text=f"Next rotation in {remaining} ticks")

        # Battery bars + cooldown labels
        max_batt = self.S.battery_max
        for u, widgets in self.batt_rows.items():
            val = self.S.battery[u]
            p = pct(val, max_batt)  # percent
            widgets["pb"].config(value=p)
            widgets["val"].config(text=f"{p}% ({val}/{max_batt})")

            cd_texts = []
            if self.S.cooldown_in[u] > 0:
                cd_texts.append(("IN", self.S.cooldown_in[u], "green"))
            if self.S.cooldown_out[u] > 0:
                cd_texts.append(("OUT", self.S.cooldown_out[u], "red"))

            if cd_texts:
                # show first priority cooldown (prefer OUT red if present)
                show = next((t for t in cd_texts if t[0] == "OUT"), cd_texts[0])
                self.batt_rows[u]["cd"].config(text=f"{show[0]}:{show[1]}", foreground=show[2])
            else:
                self.batt_rows[u]["cd"].config(text="", foreground="gray")

    def _finalize_and_exit(self):
        # Generate coverage report
        generate_coverage_report(self.coverage_records, self.S.domains, self.rotation_period_ticks,
                                 self.coverage_html_path, self.coverage_png_path)

        # Write coverage summary JSON (for CI validator)
        gaps_by_domain = {}
        total_gaps = 0
        for d in self.S.domains:
            d_gaps = sum(1 for rec in self.coverage_records if rec["domain"] == d and rec["gap"] == 1)
            gaps_by_domain[d] = d_gaps
            total_gaps += d_gaps
        import json
        with open(os.path.join(self.logs_dir, "coverage_summary.json"), "w", encoding="utf-8") as jf:
            json.dump({
                "total_gap_ticks": total_gaps,
                "gaps_by_domain": gaps_by_domain
            }, jf, indent=2)

        # Close logs
        self.evt.close()
        self.hof.close()
        self.bat.close()

        # Notify
        tk.messagebox = tk.Message(self, text=f"Simulation complete.\nReport: {self.coverage_html_path}")
        print(f"\n✅ Simulation complete.\nCoverage report: {self.coverage_html_path}")
        self.destroy()


def main():
    ap = argparse.ArgumentParser(description="AUFalkon Tkinter GUI Simulator")
    ap.add_argument("--mission", required=True, help="Path to mission JSON")
    ap.add_argument("--ticks", type=int, default=240000, help="Number of ticks to simulate")
    ap.add_argument("--step", type=int, default=1000, help="Ticks to process per UI frame")
    args = ap.parse_args()

    app = TkSimApp(args.mission, args.ticks, step_per_frame=args.step)
    app.mainloop()


if __name__ == "__main__":
    main()
