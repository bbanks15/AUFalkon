
import json
import argparse
import os
import time
import tkinter as tk
from tkinter import ttk, messagebox

from scheduler_deadline import DeadlineScheduler

class ManualFailureController:
    """
    Manual fault controller for GUI.
    Supports:
      - permanent faults (toggle)
      - temporary faults (duration ms)
    """
    def __init__(self, units):
        self.units = units[:]
        self.alive = {u: True for u in units}
        self.permanent = {u: False for u in units}
        self.recover_at_ms = {u: None for u in units}

    def set_permanent(self, unit: str, down: bool):
        self.permanent[unit] = bool(down)
        if down:
            self.alive[unit] = False
            self.recover_at_ms[unit] = None
        else:
            # If no temp fault pending, restore
            if self.recover_at_ms[unit] is None:
                self.alive[unit] = True

    def inject_temp_fault(self, unit: str, now_ms: int, duration_ms: int):
        if self.permanent.get(unit, False):
            return
        self.alive[unit] = False
        if duration_ms > 0:
            self.recover_at_ms[unit] = now_ms + int(duration_ms)
        else:
            self.recover_at_ms[unit] = None

    def tick(self, now_ms: int):
        # Recover any temp faults
        for u in self.units:
            ra = self.recover_at_ms.get(u)
            if ra is not None and now_ms >= ra and not self.permanent.get(u, False):
                self.alive[u] = True
                self.recover_at_ms[u] = None

    def status(self):
        return dict(self.alive)

class App(tk.Tk):
    def __init__(self, mission_path: str, logs_dir: str, capacity_per_unit: int = 2):
        super().__init__()
        self.title("AUFalkon Control-Layer GUI Simulator")
        self.geometry("1100x650")

        with open(mission_path, "r", encoding="utf-8") as f:
            self.mission = json.load(f)

        self.mission_path = mission_path
        self.tick_ms = float(self.mission.get("tick_ms", 1.0))
        self.max_gap_ms = int(self.mission["constraints"]["max_gap_ms"])
        self.max_gap_ticks = max(1, int(self.max_gap_ms / self.tick_ms))

        self.domains = list(self.mission["domains"])
        self.units = list(self.mission["units"])
        self.capacity_per_unit = int(capacity_per_unit)

        # per-domain required
        req_cfg = self.mission.get("required_active_per_domain", 1)
        if isinstance(req_cfg, dict):
            self.required_map = {d: int(req_cfg.get(d, 1)) for d in self.domains}
        else:
            self.required_map = {d: int(req_cfg) for d in self.domains}

        # universal roles
        universal = bool(self.mission.get("universal_roles", False))
        if universal:
            pools = {d: self.units[:] for d in self.domains}
            pools["spares"] = []
        else:
            pools = {d: self.mission.get("domain_pools", {}).get(d, []) for d in self.domains}
            pools["spares"] = self.mission.get("domain_pools", {}).get("spares", [])

        os.makedirs(logs_dir, exist_ok=True)
        self.sched = DeadlineScheduler(
            self.domains, pools, self.required_map, self.max_gap_ticks, self.tick_ms,
            capacity_per_unit=self.capacity_per_unit,
            logs_dir=logs_dir
        )

        self.fail = ManualFailureController(self.units)

        # Simulation control
        self.running = False
        self.sim_ticks_per_ui = tk.IntVar(value=10)     # run N ticks per UI cycle
        self.ui_interval_ms = tk.IntVar(value=50)       # UI update rate
        self.temp_duration_ms = tk.IntVar(value=5000)

        # State
        self.last_assignments = {d: [] for d in self.domains}
        self.multi_role_units = set()
        self.critical_failure = False
        self.failure_message = ""

        self._build_ui()
        self._update_ui_labels()

        # Start paused
        self.after(250, self._ui_loop)

    def _build_ui(self):
        # Top controls
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=8)

        ttk.Label(top, text=os.path.basename(self.mission_path), font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)

        self.btn_start = ttk.Button(top, text="Start", command=self.start)
        self.btn_start.pack(side=tk.LEFT, padx=8)

        self.btn_pause = ttk.Button(top, text="Pause", command=self.pause)
        self.btn_pause.pack(side=tk.LEFT, padx=8)

        ttk.Label(top, text="Ticks/UI").pack(side=tk.LEFT, padx=(18, 4))
        ttk.Spinbox(top, from_=1, to=200, textvariable=self.sim_ticks_per_ui, width=6).pack(side=tk.LEFT)

        ttk.Label(top, text="UI interval (ms)").pack(side=tk.LEFT, padx=(18, 4))
        ttk.Spinbox(top, from_=10, to=500, textvariable=self.ui_interval_ms, width=6).pack(side=tk.LEFT)

        ttk.Label(top, text="Temp fault duration (ms)").pack(side=tk.LEFT, padx=(18, 4))
        ttk.Spinbox(top, from_=1, to=60000, textvariable=self.temp_duration_ms, width=8).pack(side=tk.LEFT)

        # Status strip
        self.status_var = tk.StringVar(value="Ready.")
        self.status_lbl = ttk.Label(self, textvariable=self.status_var)
        self.status_lbl.pack(side=tk.TOP, fill=tk.X, padx=10)

        self.alert_var = tk.StringVar(value="")
        self.alert_lbl = tk.Label(self, textvariable=self.alert_var, bg="#1f1f1f", fg="white", font=("Segoe UI", 11, "bold"))
        self.alert_lbl.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(6, 6))

        # Main split
        main = ttk.Frame(self)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)

        left = ttk.LabelFrame(main, text="Devices (manual faults)")
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        mid = ttk.LabelFrame(main, text="Domain Coverage (live)")
        mid.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        right = ttk.LabelFrame(main, text="Assignments / Summary")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Device controls
        self.perm_vars = {}
        for u in self.units:
            row = ttk.Frame(left)
            row.pack(side=tk.TOP, fill=tk.X, padx=8, pady=4)

            ttk.Label(row, text=f"Unit {u}", width=10).pack(side=tk.LEFT)

            pv = tk.BooleanVar(value=False)
            self.perm_vars[u] = pv
            ttk.Checkbutton(row, text="Permanent Down", variable=pv, command=lambda uu=u: self._toggle_perm(uu)).pack(side=tk.LEFT, padx=6)

            ttk.Button(row, text="Temp Fault Now", command=lambda uu=u: self._temp_fault_now(uu)).pack(side=tk.LEFT, padx=6)

            # alive indicator
            ind = tk.StringVar(value="ALIVE")
            lbl = ttk.Label(row, textvariable=ind, width=10)
            lbl.pack(side=tk.LEFT, padx=6)
            setattr(self, f"_alive_ind_{u}", ind)

        # Domain table
        self.domain_tree = ttk.Treeview(mid, columns=("need", "assigned", "multi", "alive", "tick"), show="headings", height=12)
        for c, w in [("need", 70), ("assigned", 260), ("multi", 80), ("alive", 60), ("tick", 60)]:
            self.domain_tree.heading(c, text=c)
            self.domain_tree.column(c, width=w, anchor=tk.W)
        self.domain_tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Right summary
        self.summary_txt = tk.Text(right, height=28, wrap=tk.WORD)
        self.summary_txt.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.summary_txt.configure(state=tk.DISABLED)

    def _toggle_perm(self, unit: str):
        down = bool(self.perm_vars[unit].get())
        self.fail.set_permanent(unit, down)
        self._update_ui_labels()

    def _temp_fault_now(self, unit: str):
        now_ms = int(self.sched.tick * self.tick_ms)
        dur = int(self.temp_duration_ms.get())
        self.fail.inject_temp_fault(unit, now_ms, dur)
        self._update_ui_labels()

    def start(self):
        if self.critical_failure:
            messagebox.showwarning("Cannot start", "Mission is in critical failure state. Restart the app to reset.")
            return
        self.running = True
        self.status_var.set("Running...")

    def pause(self):
        self.running = False
        self.status_var.set("Paused.")

    def _ui_loop(self):
        try:
            if self.running and not self.critical_failure:
                steps = int(self.sim_ticks_per_ui.get())
                for _ in range(steps):
                    now_ms = int(self.sched.tick * self.tick_ms)
                    self.fail.tick(now_ms)

                    alive = self.fail.status()
                    # schedule one tick
                    assignments = self.sched.schedule_tick(alive)

                    # build assignment views
                    assign_map = {d: [] for d in self.domains}
                    unit_roles = {u: [] for u in self.units}
                    for d, u in assignments:
                        assign_map[d].append(u)
                        unit_roles[u].append(d)

                    self.last_assignments = assign_map
                    self.multi_role_units = {u for u, roles in unit_roles.items() if len(roles) > 1}

            self._update_ui_labels()
        except Exception as e:
            self.critical_failure = True
            self.failure_message = str(e)
            self.running = False
            self.alert_var.set(f"CRITICAL FAILURE: {self.failure_message}")
            self.alert_lbl.configure(bg="#8b0000")
            self._update_ui_labels()
            messagebox.showerror("Critical Failure", self.failure_message)

        interval = int(self.ui_interval_ms.get())
        self.after(max(10, interval), self._ui_loop)

    def _update_ui_labels(self):
        # Update alive indicators
        alive = self.fail.status()
        for u in self.units:
            ind = getattr(self, f"_alive_ind_{u}")
            if alive.get(u, False):
                ind.set("ALIVE")
            else:
                ind.set("DOWN")

        # Update domain tree
        for i in self.domain_tree.get_children():
            self.domain_tree.delete(i)

        tick = self.sched.tick
        alive_count = sum(1 for v in alive.values() if v)
        multi_units = sorted(list(self.multi_role_units))

        for d in self.domains:
            need = self.required_map[d]
            assigned = self.last_assignments.get(d, [])
            assigned_txt = ",".join(assigned)
            multi_txt = "YES" if any(u in self.multi_role_units for u in assigned) else "NO"
            self.domain_tree.insert("", tk.END, values=(need, assigned_txt, multi_txt, alive_count, tick))

        # Summary panel
        self.summary_txt.configure(state=tk.NORMAL)
        self.summary_txt.delete("1.0", tk.END)

        self.summary_txt.insert(tk.END
