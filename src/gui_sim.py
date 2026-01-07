
import json
import argparse
import os
import csv
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta

from scheduler_deadline import DeadlineScheduler


# -----------------------------
# Event stream (change-only)
# -----------------------------
class EventStream:
    """
    Human-readable, change-only event stream.

    Uses SIMULATED wall time:
      Start epoch = 01-01-26 00:00:00.000
      wall_time(tick) = epoch + sim_ms, where sim_ms = round(tick * tick_ms)

    Writes lines like:
      [tick=120 sim_ms=120 wall=01-01-26 00:00:00.120] Device D entered FAULTED state
    """
    def __init__(self, path: str, tick_ms: float, include_wall_time: bool = True):
        self.path = path
        self.tick_ms = float(tick_ms)
        self.include_wall_time = include_wall_time

        # Fixed simulation epoch (deterministic, not real time)
        self.epoch = datetime(2026, 1, 1, 0, 0, 0, 0)

        self.f = open(path, "w", encoding="utf-8")
        self.f.write("# gui_event_stream.log (change-only)\n")
        self.f.write("# format: [tick=... sim_ms=... wall=MM-DD-YY HH:MM:SS.mmm] message\n")
        self.f.write("# wall time is SIMULATED: epoch(01-01-26 00:00:00.000) + sim_ms\n\n")

        self.prev_alive = None           # dict unit->bool
        self.prev_roles = {}             # unit -> set(domains)
        self.prev_rest = set()           # set(units)
        self.prev_handoff_sig = set()    # set of (domain, tuple(removed), tuple(added)) at previous tick

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass

    def _sim_ms(self, tick: int) -> int:
        return int(round(tick * self.tick_ms))

    def _fmt_wall(self, sim_ms: int) -> str:
        dt = self.epoch + timedelta(milliseconds=sim_ms)
        ms = dt.microsecond // 1000
        return f"{dt.strftime('%m-%d-%y %H:%M:%S')}.{ms:03d}"

    def _stamp(self, tick: int) -> str:
        sim_ms = self._sim_ms(tick)
        if not self.include_wall_time:
            return f"[tick={tick} sim_ms={sim_ms}]"
        return f"[tick={tick} sim_ms={sim_ms} wall={self._fmt_wall(sim_ms)}]"

    def emit(self, tick: int, msg: str):
        self.f.write(f"{self._stamp(tick)} {msg}\n")
        self.f.flush()

    def seed(self, tick: int, alive: dict, roles: dict, rest_units: set):
        """Set baseline so we don't emit duplicates on first update."""
        self.prev_alive = dict(alive)
        self.prev_roles = {u: set(roles.get(u, set())) for u in alive.keys()}
        self.prev_rest = set(rest_units)
        self.prev_handoff_sig = set()

    def update(self, tick: int, alive: dict, assignments: list, handoffs: list):
        """
        Compare state to previous baseline and emit change-only narrative events.
        Also emits atomic handoff summary lines when handoffs change.
        """
        # Build roles map from assignments
        roles = {u: set() for u in alive.keys()}
        for d, u in assignments:
            roles.setdefault(u, set()).add(d)

        alive_units = {u for u, ok in alive.items() if ok}
        assigned_units = {u for _d, u in assignments}
        rest_units = alive_units - assigned_units

        # 1) Alive transitions (DOWN/UP)
        if self.prev_alive is not None:
            for u in alive.keys():
                was = bool(self.prev_alive.get(u, False))
                now = bool(alive.get(u, False))
                if was and not now:
                    self.emit(tick, f"Device {u} entered FAULTED state")
                if (not was) and now:
                    # Prefer combined message if it recovered into REST
                    if u in rest_units:
                        self.emit(tick, f"Device {u} recovered and entered REST state")
                    else:
                        entered = sorted(list(roles.get(u, set())))
                        if entered:
                            self.emit(tick, f"Device {u} recovered and entered {entered[0]}")
                        else:
                            self.emit(tick, f"Device {u} recovered")

        # 2) Role transitions per device (entered/exited domain)
        for u in alive.keys():
            prev = self.prev_roles.get(u, set())
            curr = roles.get(u, set())
            entered = sorted(list(curr - prev))
            exited = sorted(list(prev - curr))

            for d in entered:
                self.emit(tick, f"Device {u} entered {d}")
            for d in exited:
                self.emit(tick, f"Device {u} exited {d}")

        # 3) REST transitions
        prev_rest = self.prev_rest
        entered_rest = sorted(list(rest_units - prev_rest))
        exited_rest = sorted(list(prev_rest - rest_units))

        for u in entered_rest:
            self.emit(tick, f"Device {u} entered REST state")
        for u in exited_rest:
            self.emit(tick, f"Device {u} exited REST state")

        # 4) Atomic handoff summary (change-only)
        # handoffs should be list of dicts: {domain, removed, added, atomic}
        curr_sig = set()
        for h in (handoffs or []):
            dom = h.get("domain", "")
            rem = tuple(h.get("removed", []) or [])
            add = tuple(h.get("added", []) or [])
            curr_sig.add((dom, rem, add))

        # Emit handoff lines only for new/changed handoffs
        new_handoffs = curr_sig - self.prev_handoff_sig
        for (dom, rem, add) in sorted(list(new_handoffs)):
            rem_list = list(rem)
            add_list = list(add)
            self.emit(
                tick,
                f"Atomic handoff completed for {dom}: removed={rem_list} added={add_list}"
            )

        # Save for next tick
        self.prev_alive = dict(alive)
        self.prev_roles = {u: set(roles.get(u, set())) for u in alive.keys()}
        self.prev_rest = set(rest_units)
        self.prev_handoff_sig = curr_sig


# -----------------------------
# Manual fault controller
# -----------------------------
class ManualFailureController:
    """
    Manual fault controller for GUI.
    Supports:
      - permanent faults (toggle)
      - temporary faults (duration ms, sim-time)
      - immediate recovery (per device and recover all)
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

    def recover_now(self, unit: str):
        """
        Force immediate recovery: clears permanent + temporary fault state and sets alive=True.
        """
        if unit not in self.alive:
            return
        self.permanent[unit] = False
        self.recover_at_ms[unit] = None
        self.alive[unit] = True

    def recover_all(self):
        for u in self.units:
            self.recover_now(u)

    def tick(self, now_ms: int):
        # Recover any temp faults when sim time advances
        for u in self.units:
            ra = self.recover_at_ms.get(u)
            if ra is not None and now_ms >= ra and not self.permanent.get(u, False):
                self.alive[u] = True
                self.recover_at_ms[u] = None

    def status(self):
        return dict(self.alive)

    def state_label(self, unit: str, now_ms: int) -> str:
        """
        ALIVE / PERM_DOWN / TEMP_DOWN / DOWN (fallback)
        """
        if self.permanent.get(unit, False):
            return "PERM_DOWN"
        if not self.alive.get(unit, True):
            ra = self.recover_at_ms.get(unit)
            if ra is not None and now_ms < ra:
                return "TEMP_DOWN"
            return "DOWN"
        return "ALIVE"


# -----------------------------
# GUI App
# -----------------------------
class App(tk.Tk):
    def __init__(self, mission_path: str, logs_dir: str, capacity_per_unit: int = 2):
        super().__init__()
        self.title("AUFalkon Control-Layer GUI Simulator")
        self.geometry("1200x720")

        with open(mission_path, "r", encoding="utf-8") as f:
            self.mission = json.load(f)

        self.mission_path = mission_path
        self.logs_dir = logs_dir

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

        # configured spares (for display)
        self.configured_spares = list(self.mission.get("domain_pools", {}).get("spares", []))

        # universal roles
        universal = bool(self.mission.get("universal_roles", False))
        if universal:
            pools = {d: self.units[:] for d in self.domains}
            pools["spares"] = []
        else:
            pools = {d: self.mission.get("domain_pools", {}).get(d, []) for d in self.domains}
            pools["spares"] = self.mission.get("domain_pools", {}).get("spares", [])

        os.makedirs(logs_dir, exist_ok=True)

        # Scheduler
        self.sched = DeadlineScheduler(
            self.domains, pools, self.required_map, self.max_gap_ticks, self.tick_ms,
            capacity_per_unit=self.capacity_per_unit,
            logs_dir=logs_dir
        )

        # Manual fault model
        self.fail = ManualFailureController(self.units)

        # GUI action log
        self.action_log_path = os.path.join(logs_dir, "gui_actions.csv")
        self.action_log_f = open(self.action_log_path, "w", newline="", encoding="utf-8")
        self.action_log_w = csv.writer(self.action_log_f)
        self.action_log_w.writerow(["tick", "sim_ms", "action", "unit", "details"])

        # GUI event stream log (change-only narrative)
        self.event_stream = EventStream(
            os.path.join(logs_dir, "gui_event_stream.log"),
            tick_ms=self.tick_ms,
            include_wall_time=True
        )

        # Simulation control
        self.running = False
        self.sim_ticks_per_ui = tk.IntVar(value=10)     # run N ticks per UI cycle
        self.ui_interval_ms = tk.IntVar(value=50)       # UI update rate
        self.temp_duration_ms = tk.IntVar(value=5000)

        # State
        self.last_assignments = {d: [] for d in self.domains}
        self.multi_role_units = set()
        self.rest_units = set(self.units)  # alive but unassigned
        self.critical_failure = False
        self.failure_message = ""

        self._build_ui()

        # Seed event stream baseline at tick=0
        alive0 = self.fail.status()
        roles0 = {u: set() for u in alive0.keys()}
        rest0 = {u for u, ok in alive0.items() if ok}
        self.event_stream.seed(self.sched.tick, alive0, roles0, rest0)

        self._update_ui_labels()

        # Start paused
        self.after(250, self._ui_loop)

    # ----- logging helpers -----
    def _sim_ms(self, tick: int) -> int:
        return int(round(tick * self.tick_ms))

    def _log_action(self, action: str, unit: str = "", details: str = ""):
        tick = self.sched.tick
        self.action_log_w.writerow([tick, self._sim_ms(tick), action, unit, details])
        self.action_log_f.flush()

    def _sync_event_stream_baseline(self):
        """
        After manual actions (fault/recover), sync baseline so we don't double-log
        the same transitions on the next scheduler tick.
        """
        alive = self.fail.status()

        # Build roles from last assignments
        roles = {u: set() for u in alive.keys()}
        for d in self.domains:
            for u in self.last_assignments.get(d, []):
                roles.setdefault(u, set()).add(d)

        alive_units = {u for u, ok in alive.items() if ok}
        assigned_units = {u for d in self.domains for u in self.last_assignments.get(d, [])}
        rest_units = alive_units - assigned_units

        self.event_stream.seed(self.sched.tick, alive, roles, rest_units)

    # ----- UI -----
    def _build_ui(self):
        top = ttk.Frame(self)
        top.pack(side=tk.TOP, fill=tk.X, padx=10, pady=8)

        ttk.Label(top, text=os.path.basename(self.mission_path), font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)

        self.btn_start = ttk.Button(top, text="Start", command=self.start)
        self.btn_start.pack(side=tk.LEFT, padx=8)

        self.btn_pause = ttk.Button(top, text="Pause", command=self.pause)
        self.btn_pause.pack(side=tk.LEFT, padx=8)

        self.btn_recover_all = ttk.Button(top, text="Recover ALL", command=self.recover_all)
        self.btn_recover_all.pack(side=tk.LEFT, padx=(20, 8))

        ttk.Label(top, text="Ticks/UI").pack(side=tk.LEFT, padx=(18, 4))
        ttk.Spinbox(top, from_=1, to=200, textvariable=self.sim_ticks_per_ui, width=6).pack(side=tk.LEFT)

        ttk.Label(top, text="UI interval (ms)").pack(side=tk.LEFT, padx=(18, 4))
        ttk.Spinbox(top, from_=10, to=500, textvariable=self.ui_interval_ms, width=6).pack(side=tk.LEFT)

        ttk.Label(top, text="Temp fault duration (ms)").pack(side=tk.LEFT, padx=(18, 4))
        ttk.Spinbox(top, from_=1, to=60000, textvariable=self.temp_duration_ms, width=8).pack(side=tk.LEFT)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var).pack(side=tk.TOP, fill=tk.X, padx=10)

        self.alert_var = tk.StringVar(value="")
        self.alert_lbl = tk.Label(
            self,
            textvariable=self.alert_var,
            bg="#1f1f1f",
            fg="white",
            font=("Segoe UI", 11, "bold")
        )
        self.alert_lbl.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(6, 6))

        main = ttk.Frame(self)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=10)

        left = ttk.LabelFrame(main, text="Devices (manual faults)")
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        mid = ttk.LabelFrame(main, text="Domain Coverage (live)")
        mid.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        right = ttk.LabelFrame(main, text="Assignments / Summary (incl. REST + spares + handoffs)")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Device controls
        self.perm_vars = {}
        for u in self.units:
            row = ttk.Frame(left)
            row.pack(side=tk.TOP, fill=tk.X, padx=8, pady=4)

            ttk.Label(row, text=f"Unit {u}", width=10).pack(side=tk.LEFT)

            pv = tk.BooleanVar(value=False)
            self.perm_vars[u] = pv

            ttk.Checkbutton(
                row,
                text="Permanent Down",
                variable=pv,
                command=lambda uu=u: self._toggle_perm(uu)
            ).pack(side=tk.LEFT, padx=6)

            ttk.Button(
                row,
                text="Temp Fault Now",
                command=lambda uu=u: self._temp_fault_now(uu)
            ).pack(side=tk.LEFT, padx=6)

            ttk.Button(
                row,
                text="Recover",
                command=lambda uu=u: self._recover_unit(uu)
            ).pack(side=tk.LEFT, padx=6)

            ind = tk.StringVar(value="ALIVE")
            ttk.Label(row, textvariable=ind, width=10).pack(side=tk.LEFT, padx=6)
            setattr(self, f"_alive_ind_{u}", ind)

        # Domain table
        self.domain_tree = ttk.Treeview(
            mid,
            columns=("need", "assigned", "multi", "alive", "tick"),
            show="headings",
            height=14
        )
        for c, w in [("need", 70), ("assigned", 300), ("multi", 90), ("alive", 60), ("tick", 70)]:
            self.domain_tree.heading(c, text=c)
            self.domain_tree.column(c, width=w, anchor=tk.W)
        self.domain_tree.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)

        # Right summary
        self.summary_txt = tk.Text(right, height=34, wrap=tk.WORD)
        self.summary_txt.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.summary_txt.configure(state=tk.DISABLED)

    # ----- actions -----
    def _toggle_perm(self, unit: str):
        down = bool(self.perm_vars[unit].get())
        self.fail.set_permanent(unit, down)
        self._log_action("PERM_DOWN_SET" if down else "PERM_DOWN_CLEAR", unit)

        # Emit a clean narrative line immediately at current tick
        if down:
            self.event_stream.emit(self.sched.tick, f"Device {unit} entered FAULTED state")
        else:
            # Clearing permanent doesn't necessarily mean alive if temp fault pending; reflect state:
            state = self.fail.status().get(unit, False)
            if state:
                # If alive now, treat as recovered
                self.event_stream.emit(self.sched.tick, f"Device {unit} recovered")
        self._sync_event_stream_baseline()
        self._update_ui_labels()

    def _temp_fault_now(self, unit: str):
        now_ms = self._sim_ms(self.sched.tick)
        dur = int(self.temp_duration_ms.get())
        self.fail.inject_temp_fault(unit, now_ms, dur)
        self._log_action("TEMP_FAULT", unit, f"duration_ms={dur}")

        self.event_stream.emit(self.sched.tick, f"Device {unit} entered FAULTED state")
        self._sync_event_stream_baseline()
        self._update_ui_labels()

    def _recover_unit(self, unit: str):
        self.fail.recover_now(unit)
        if unit in self.perm_vars:
            self.perm_vars[unit].set(False)
        self._log_action("RECOVER_NOW", unit)

        # Immediate narrative (rest/role will be captured after next scheduling tick)
        self.event_stream.emit(self.sched.tick, f"Device {unit} recovered")
        self._sync_event_stream_baseline()
        self._update_ui_labels()

    def recover_all(self):
        self.fail.recover_all()
        for u in self.units:
            if u in self.perm_vars:
                self.perm_vars[u].set(False)
        self._log_action("RECOVER_ALL")

        self.event_stream.emit(self.sched.tick, "All devices recovered")
        self._sync_event_stream_baseline()
        self._update_ui_labels()

    # ----- sim control -----
    def start(self):
        if self.critical_failure:
            messagebox.showwarning(
                "Cannot start",
                "Mission is in critical failure state. Restart the app to reset."
            )
            return
        self.running = True
        self.status_var.set("Running...")

    def pause(self):
        self.running = False
        self.status_var.set("Paused.")

    # ----- main loop -----
    def _ui_loop(self):
        try:
            if self.running and not self.critical_failure:
                steps = int(self.sim_ticks_per_ui.get())
                for _ in range(steps):
                    now_ms = self._sim_ms(self.sched.tick)
                    self.fail.tick(now_ms)

                    alive = self.fail.status()
                    assignments = self.sched.schedule_tick(alive)

                    # build assignment views
                    assign_map = {d: [] for d in self.domains}
                    unit_roles = {u: [] for u in self.units}
                    for d, u in assignments:
                        assign_map[d].append(u)
                        unit_roles[u].append(d)

                    self.last_assignments = assign_map
                    self.multi_role_units = {u for u, roles in unit_roles.items() if len(roles) > 1}

                    # REST units (alive but unassigned)
                    assigned_units = set()
                    for d in self.domains:
                        assigned_units |= set(assign_map[d])
                    alive_units = {u for u, ok in alive.items() if ok}
                    self.rest_units = alive_units - assigned_units

                    # Pull handoffs from scheduler (if available)
                    handoffs = []
                    if hasattr(self.sched, "get_last_tick_details"):
                        details = self.sched.get_last_tick_details() or {}
                        handoffs = details.get("handoffs", []) or []

                    # Update narrative log AFTER scheduling (captures who filled in)
                    self.event_stream.update(self.sched.tick, alive, assignments, handoffs)

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

    # ----- render -----
    def _update_ui_labels(self):
        alive = self.fail.status()
        now_ms = self._sim_ms(self.sched.tick)

        # unit indicators
        for u in self.units:
            ind = getattr(self, f"_alive_ind_{u}")
            ind.set(self.fail.state_label(u, now_ms))

        # domain tree
        for i in self.domain_tree.get_children():
            self.domain_tree.delete(i)

        tick = self.sched.tick
        alive_count = sum(1 for v in alive.values() if v)

        for d in self.domains:
            need = self.required_map[d]
            assigned = self.last_assignments.get(d, [])
            assigned_txt = ",".join(assigned)
            multi_txt = "YES" if any(u in self.multi_role_units for u in assigned) else "NO"
            self.domain_tree.insert("", tk.END, values=(need, assigned_txt, multi_txt, alive_count, tick))

        # summary
        self.summary_txt.configure(state=tk.NORMAL)
        self.summary_txt.delete("1.0", tk.END)

        # Compute spare availability
        alive_units = {u for u, ok in alive.items() if ok}
        alive_spares = sorted(list(set(self.configured_spares) & alive_units))
        rest_spares = sorted(list(set(alive_spares) & set(self.rest_units)))

        multi_units = sorted(list(self.multi_role_units))
        rest_units = sorted(list(self.rest_units))

        # Simulated wall time string
        sim_ms = self._sim_ms(tick)
        epoch = datetime(2026, 1, 1, 0, 0, 0, 0)
        wall_dt = epoch + timedelta(milliseconds=sim_ms)
        wall_ms = wall_dt.microsecond // 1000
        wall_str = f"{wall_dt.strftime('%m-%d-%y %H:%M:%S')}.{wall_ms:03d}"

        self.summary_txt.insert(tk.END, f"Tick: {tick}   sim_ms: {sim_ms}   wall(sim): {wall_str}\n")
        self.summary_txt.insert(tk.END, f"Alive units: {alive_count} / {len(self.units)}\n\n")

        self.summary_txt.insert(tk.END, f"Configured spares (mission): {self.configured_spares if self.configured_spares else 'None'}\n")
        self.summary_txt.insert(tk.END, f"Alive spares: {alive_spares if alive_spares else 'None'}\n")
        self.summary_txt.insert(tk.END, f"Alive spares in REST: {rest_spares if rest_spares else 'None'}\n")
        self.summary_txt.insert(tk.END, f"REST units (alive but unassigned this tick): {rest_units if rest_units else 'None'}\n")
        self.summary_txt.insert(tk.END, f"Multi-role units (contingency indicator): {multi_units if multi_units else 'None'}\n\n")

        self.summary_txt.insert(tk.END, "Per-domain assignments:\n")
        for d in self.domains:
            self.summary_txt.insert(
                tk.END,
                f"  - {d} need={self.required_map[d]} assigned={self.last_assignments.get(d, [])}\n"
            )

        # Scheduler summary (contingency)
        if hasattr(self.sched, "get_run_summary"):
            rs = self.sched.get_run_summary() or {}
            ct = rs.get("contingency_ticks")
            tt = rs.get("total_ticks")
            mode = rs.get("last_mode")
            if ct is not None and tt is not None:
                rate = (ct / max(1, tt)) * 100.0
                self.summary_txt.insert(tk.END, f"\nMode: {mode}\n")
                self.summary_txt.insert(tk.END, f"Contingency ticks: {ct}/{tt} ({rate:.1f}%)\n")

        # Last tick handoffs (atomic evidence)
        if hasattr(self.sched, "get_last_tick_details"):
            details = self.sched.get_last_tick_details() or {}
            handoffs = details.get("handoffs", []) or []
            if handoffs:
                self.summary_txt.insert(tk.END, "\nLast tick handoffs (atomic):\n")
                for h in handoffs:
                    self.summary_txt.insert(
                        tk.END,
                        f"  - domain={h.get('domain')} removed={h.get('removed')} added={h.get('added')} atomic={h.get('atomic')}\n"
                    )

        self.summary_txt.configure(state=tk.DISABLED)

    def destroy(self):
        try:
            self.action_log_f.close()
        except Exception:
            pass
        try:
            self.event_stream.close()
        except Exception:
            pass
        try:
            self.sched.close()
        except Exception:
            pass
        super().destroy()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mission", required=True, help="Path to mission JSON")
    ap.add_argument("--logs_dir", default="gui_logs", help="Where to write logs")
    ap.add_argument("--capacity_per_unit", type=int, default=2)
    args = ap.parse_args()

    app = App(args.mission, args.logs_dir, capacity_per_unit=args.capacity_per_unit)
    app.mainloop()


if __name__ == "__main__":
    main()
