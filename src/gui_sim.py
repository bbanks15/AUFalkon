
# src/gui_sim.py
# GUI Simulator for AUFalkon control-layer demo
# - Loads mission & config (with auto discovery if none)
# - Runs DeadlineScheduler ticks
# - Detects coverage gaps in GUI
# - Non-blocking banner on gap start; hard-fail if unresolved after grace
# - Optional per-domain battery drain badges
#
# Brooks Banks — Cary, NC

import os
import sys
import json
import time
import argparse
import glob
from datetime import datetime

try:
    import tkinter as tk
    from tkinter import ttk
except Exception as e:
    raise RuntimeError(f"Tkinter not available: {e}")

# Optional YAML parsing
def _load_yaml(path):
    try:
        import yaml  # PyYAML
    except ImportError:
        yaml = None

    if yaml and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    # Fallback: simple key: value parser
    data = {}
    if not os.path.exists(path):
        return data
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if v.lower() in ("true", "false"):
                    data[k] = (v.lower() == "true")
                else:
                    try:
                        if "." in v:
                            data[k] = float(v)
                        else:
                            data[k] = int(v)
                    except:
                        data[k] = v
    return data

# Utilities
def _now_iso():
    return datetime.now().isoformat(timespec="seconds")

def _ensure_dir(p):
    os.makedirs(p, exist_ok=True)

def _discover_mission(default_dir="missions"):
    """Return the first *.json mission path in default_dir, or None if none found."""
    try:
        candidates = sorted(glob.glob(os.path.join(default_dir, "*.json")))
        return candidates[0] if candidates else None
    except Exception:
        return None

# Mission loader (JSON assumed)
def _load_mission(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# Import scheduler
try:
    from src.scheduler_deadline import DeadlineScheduler
except Exception as e:
    raise RuntimeError(f"Failed to import DeadlineScheduler: {e}")

class GuiSimApp:
    def __init__(self, args):
        # Config precedence: CLI overrides YAML
        cfg_path = getattr(args, "config", None) or os.environ.get("GUI_SIM_CONFIG", "configs/gui_sim.yaml")
        cfg = _load_yaml(cfg_path) if os.path.exists(cfg_path) else {}

        # Resolve mission path: CLI → YAML → discovery
        mission_path = args.mission or cfg.get("mission") or _discover_mission("missions")
        if not mission_path:
            raise ValueError(
                "Mission path not specified and none auto-discovered.\n"
                "Provide --mission <path> or create configs/gui_sim.yaml with a 'mission:' key.\n"
                "Tip: place your mission JSONs under ./missions/ for auto-discovery."
            )
        if not os.path.exists(mission_path):
            raise FileNotFoundError(f"Mission file not found: {mission_path}")
        self.mission = _load_mission(mission_path)

        # Log dir
        self.logs_dir = args.logs_dir or cfg.get("logs_dir", "gui_logs")
        _ensure_dir(self.logs_dir)

        # Runtime knobs
        self.tick_ms = int(args.tick_ms or cfg.get("tick_ms", 1000))
        self.capacity_per_unit = int(args.capacity_per_unit or cfg.get("capacity_per_unit", 2))

        # Rotation/cooldown tuning
        self.rotation_weight = float(args.rotation_weight if args.rotation_weight is not None else cfg.get("rotation_weight", 0.40))
        self.cooldown_weight = float(args.cooldown_weight if args.cooldown_weight is not None else cfg.get("cooldown_weight", 0.60))
        self.min_dwell_ticks = int(args.min_dwell_ticks if args.min_dwell_ticks is not None else cfg.get("min_dwell_ticks", 30))
        self.hysteresis_pct = float(args.hysteresis_pct if args.hysteresis_pct is not None else cfg.get("hysteresis_pct", 0.08))
        self.battery_reserve_pct = float(args.battery_reserve_pct if args.battery_reserve_pct is not None else cfg.get("battery_reserve_pct", 0.15))

        # Gap handling
        self.fail_on_gap = bool(args.fail_on_gap or cfg.get("fail_on_gap", False))
        self.gap_recovery_ticks = int(args.gap_recovery_ticks or cfg.get("gap_recovery_ticks", 100))

        # GUI options
        self.show_domain_drain_badges = bool(cfg.get("show_domain_drain_badges", True))
        self.show_gap_banner = bool(cfg.get("show_gap_banner", True))

        # Domain costs (for badges/fallbacks)
        self.domain_costs = self.mission.get("domain_costs", {"radar": 3, "comm": 2, "network": 1})
        self.domains = list(self.domain_costs.keys())

        # Root window
        self.root = tk.Tk()
        self.root.title(f"AUFalkon Control-Layer Demo — {os.path.basename(mission_path)}")

        # State
        self._tick_counter = 0
        self.domain_faults = {d: False for d in self.domains}
        self._gap_banner = None
        self._gap_active = False
        self._gap_start_tick = None

        # Logs
        self._setup_logs()

        # Scheduler — weights applied
        self.scheduler = DeadlineScheduler(
            mission=self.mission,
            tick_ms=self.tick_ms,
            rotation_weight=self.rotation_weight,
            cooldown_weight=self.cooldown_weight,
            min_dwell_ticks=self.min_dwell_ticks,
            hysteresis_pct=self.hysteresis_pct,
            battery_reserve_pct=self.battery_reserve_pct,
            capacity_per_unit=self.capacity_per_unit
        )

        # Build UI
        self._build_layout()

        # Tick timer
        self._tick_job = None

    # -------- Logs ----------
    def _setup_logs(self):
        self.event_log_path = os.path.join(self.logs_dir, "gui_event_stream.log")
        self.actions_csv_path = os.path.join(self.logs_dir, "gui_actions.csv")
        self.gap_events_csv_path = os.path.join(self.logs_dir, "gap_events.csv")
        # Init CSV headers
        if not os.path.exists(self.actions_csv_path):
            with open(self.actions_csv_path, "w", encoding="utf-8") as f:
                f.write("timestamp,action,detail\n")
        if not os.path.exists(self.gap_events_csv_path):
            with open(self.gap_events_csv_path, "w", encoding="utf-8") as f:
                f.write("timestamp,event,domains,tick\n")

    def _log_event(self, msg):
        with open(self.event_log_path, "a", encoding="utf-8") as f:
            f.write(f"{_now_iso()} {msg}\n")

    def _log_action(self, action, detail=""):
        with open(self.actions_csv_path, "a", encoding="utf-8") as f:
            f.write(f"{_now_iso()},{action},{detail}\n")

    def _log_gap_event(self, kind, domains):
        with open(self.gap_events_csv_path, "a", encoding="utf-8") as f:
            f.write(f"{_now_iso()},{kind},{';'.join(domains)},{self._tick_counter}\n")

    # -------- UI ----------
    def _build_layout(self):
        outer = ttk.Frame(self.root, padding=8)
        outer.pack(fill="both", expand=True)

        # Top: Controls
        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        ttk.Label(controls, text="Manual Fault Controls:").pack(side="left")

        # Toggle faults
        self._fault_vars = {}
        for d in self.domains:
            var = tk.BooleanVar(value=False)
            chk = ttk.Checkbutton(controls, text=f"{d} fault", variable=var, command=lambda dom=d, v=var: self._toggle_fault(dom, v.get()))
            chk.pack(side="left", padx=4)
            self._fault_vars[d] = var

        # Middle: Battery bars + rotation/cooldown info
        main = ttk.Frame(outer)
        main.pack(fill="both", expand=True, pady=6)

        left = ttk.Frame(main)
        left.pack(side="left", fill="y", padx=10)

        ttk.Label(left, text="Units & Battery").pack(anchor="w")
        self._unit_bars = {}  # unit_id -> dict(labels)
        for uid in self.scheduler.get_unit_ids():
            frame = ttk.Frame(left)
            frame.pack(fill="x", pady=2)
            name = ttk.Label(frame, text=uid, width=12)
            name.pack(side="left")
            bar = ttk.Progressbar(frame, orient="horizontal", length=200, mode="determinate")
            bar.pack(side="left", padx=6)
            pct_lbl = ttk.Label(frame, text="0%")
            pct_lbl.pack(side="left")
            self._unit_bars[uid] = {"bar": bar, "pct": pct_lbl, "name": name}

        # Right: Domain table with optional drain badges
        right = ttk.Frame(main)
        right.pack(side="left", fill="both", expand=True, padx=10)

        ttk.Label(right, text="Domain Status").pack(anchor="w")
        self._domain_rows = {}  # domain -> dict widgets
        for d in self.domains:
            row = ttk.Frame(right)
            row.pack(fill="x", pady=2)
            name = ttk.Label(row, text=d, width=12)
            name.pack(side="left")
            status = ttk.Label(row, text="OK", width=8)
            status.pack(side="left")
            badge = ttk.Label(row, text="drain: 0.00%", width=14, relief="groove")
            if self.show_domain_drain_badges:
                badge.pack(side="left", padx=6)
            self._domain_rows[d] = {"name": name, "status": status, "badge": badge}

        # Bottom: Summary
        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=6)
        self._summary_lbl = ttk.Label(bottom, text="Ready")
        self._summary_lbl.pack(side="left")

    def _toggle_fault(self, domain, val):
        self.domain_faults[domain] = bool(val)
        self._log_action("fault_toggle", f"{domain}={val}")

    # -------- Coverage Map (GUI-level) ----------
    def _unit_can_cover(self, unit_id, domain):
        """
        Conservative default: if unit capabilities exist in mission, honor them; otherwise assume True.
        mission schema example:
          mission["units"][i]["capabilities"] = ["radar","comm"]
        """
        units = self.mission.get("units") or []
        caps = None
        for u in units:
            uid = u.get("id") or u.get("name")
            if uid == unit_id:
                caps = u.get("capabilities")
                break
        if isinstance(caps, list):
            return domain in caps
        return True

    def get_coverage_map(self):
        """
        Returns {domain: bool} computed in the GUI, factoring manual faults and unit capabilities.
        If no active unit, everything is uncovered.
        """
        active = self.scheduler.current_unit_id()
        if active is None:
            return {d: False for d in self.domains}
        cov = {}
        for d in self.domains:
            fault = self.domain_faults.get(d, False)
            cov[d] = (not fault) and self._unit_can_cover(active, d)
        return cov

    # -------- Banner ----------
    def _show_gap_banner(self, uncovered, critical=False):
        if not self.show_gap_banner:
            return
        try:
            if self._gap_banner:
                self._gap_banner.destroy()
        except:
            pass
        self._gap_banner = tk.Toplevel(self.root)
        self._gap_banner.title("Coverage Gap")
        self._gap_banner.attributes("-topmost", True)
        x = self.root.winfo_rootx() + 40
        y = self.root.winfo_rooty() + 40
        self._gap_banner.geometry(f"+{x}+{y}")

        bg = "#ffcc00" if not critical else "#ff6666"
        frame = tk.Frame(self._gap_banner, bg=bg, padx=12, pady=10)
        frame.pack(fill="both", expand=True)
        msg = f"Coverage gap detected: {', '.join(uncovered)}. Attempting recovery..."
        if critical:
            msg = f"CRITICAL: gap unresolved after {self.gap_recovery_ticks} ticks.\nDomains: {', '.join(uncovered)}"
        tk.Label(frame, text=msg, bg=bg, fg="#000", font=("Segoe UI", 11, "bold")).pack()

    def _hide_gap_banner(self):
        if self._gap_banner:
            try:
                self._gap_banner.destroy()
            except:
                pass
            self._gap_banner = None

    # -------- Tick Loop ----------
    def start(self):
        # kick off periodic ticks
        self._schedule_next_tick()

    def _schedule_next_tick(self):
        self._tick_job = self.root.after(self.tick_ms, self._on_tick)

    def _on_tick(self):
        try:
            self.scheduler.step()
            self._tick_counter += 1

            coverage_map = self.get_coverage_map()
            uncovered = [d for d, ok in coverage_map.items() if not ok]

            # Gap watcher
            if uncovered and not self._gap_active:
                self._gap_active = True
                self._gap_start_tick = self._tick_counter
                self._show_gap_banner(uncovered, critical=False)
                self._log_gap_event("gap_start", uncovered)
                self._log_event(f"[GAP] start domains={uncovered}")

            elif not uncovered and self._gap_active:
                self._gap_active = False
                self._hide_gap_banner()
                self._log_gap_event("gap_end", [])
                self._log_event(f"[GAP] end")

            # Hard fail if configured and grace expired
            if self.fail_on_gap and self._gap_active:
                if (self._tick_counter - self._gap_start_tick) >= self.gap_recovery_ticks:
                    self._show_gap_banner(uncovered, critical=True)
                    self._log_gap_event("gap_fail", uncovered)
                    self._log_event(f"[GAP] fail after {self.gap_recovery_ticks} ticks; domains={uncovered}")

                    # Generate coverage report on fail
                    try:
                        from src.mission_runner import generate_coverage_report
                        generate_coverage_report(self.logs_dir)
                    except Exception as e:
                        self._log_event(f"[ERROR] coverage_report on fail: {e}")

                    # Exit non-zero to flag CI
                    self.root.after(1200, lambda: sys.exit(2))
                    return

            # Update UI
            self._update_battery_bars()
            self._update_domain_table(coverage_map)
            if self.show_domain_drain_badges:
                self._update_drain_badges()

            # Summary
            active = self.scheduler.current_unit_id()
            rot_rem_ms = self.scheduler.rotation_remaining_ms()
            self._summary_lbl.configure(text=f"tick={self._tick_counter} active={active} rotation_remaining={rot_rem_ms}ms")

        except Exception as ex:
            self._log_event(f"[ERROR] {ex}")
            raise
        finally:
            # schedule next tick
            if self._tick_job is not None:
                self._schedule_next_tick()

    def _update_battery_bars(self):
        for uid in self.scheduler.get_unit_ids():
            pct = self.scheduler.get_unit_battery_pct(uid) * 100.0
            widgets = self._unit_bars.get(uid)
            if not widgets:
                continue
            widgets["bar"].configure(value=pct)
            widgets["pct"].configure(text=f"{pct:.1f}%")
            # highlight active
            if uid == self.scheduler.current_unit_id():
                widgets["name"].configure(foreground="#0066cc")
            else:
                widgets["name"].configure(foreground="#000000")

    def _update_domain_table(self, coverage_map):
        for d, w in self._domain_rows.items():
            ok = coverage_map.get(d, False)
            w["status"].configure(text=("OK" if ok else "GAP"),
                                  foreground=("#118811" if ok else "#cc0000"))

    def _update_drain_badges(self):
        breakdown = getattr(self.scheduler, "last_interval_drain_breakdown", {}) or {}
        # fallback using domain_costs if breakdown empty
        if not breakdown:
            drain_total = getattr(self.scheduler, "last_interval_drain", 0.0)
            wsum = sum(self.domain_costs.values()) or 1
            breakdown = {d: drain_total * (self.domain_costs[d] / wsum) for d in self.domains}
        for d, w in self._domain_rows.items():
            val = breakdown.get(d, 0.0)
            pct = (val / 100000.0) * 100.0  # normalized percent of 100k
            txt = f"drain: {pct:.2f}%"
            w["badge"].configure(text=txt)
            # color scale
            if pct < 0.08:
                bg = "#c7f5c7"  # greenish
            elif pct < 0.25:
                bg = "#ffe6a3"  # amber
            else:
                bg = "#ffb3b3"  # red
            w["badge"].configure(background=bg)

    # -------- Entrypoint ----------
    def run(self):
        self.start()
        self.root.mainloop()

# -------- CLI ----------
def build_arg_parser():
    p = argparse.ArgumentParser(description="AUFalkon GUI Simulator")
    p.add_argument("--mission", type=str, help="Path to mission JSON.")
    p.add_argument("--logs_dir", type=str, help="Directory for GUI logs.")
    p.add_argument("--capacity_per_unit", type=int, help="Capacity per unit.")
    p.add_argument("--tick_ms", type=int, help="Tick duration in milliseconds.")
    p.add_argument("--config", type=str, default="configs/gui_sim.yaml", help="Path to GUI sim YAML config.")

    # Rotation/cooldown tuning
    p.add_argument("--rotation_weight", type=float, help="Weight for rotation desirability.")
    p.add_argument("--cooldown_weight", type=float, help="Weight for cooldown penalty.")
    p.add_argument("--min_dwell_ticks", type=int, help="Minimum ticks before rotation eligible.")
    p.add_argument("--hysteresis_pct", type=float, help="Required score improvement to rotate.")
    p.add_argument("--battery_reserve_pct", type=float, help="Reserve threshold for rotation.")

    # Gap handling
    p.add_argument("--fail_on_gap", action="store_true", help="Hard-fail if gap persists after grace ticks.")
    p.add_argument("--gap_recovery_ticks", type=int, help="Grace ticks before hard fail.")
    return p

def main():
    parser = build_arg_parser()
    args = parser.parse_args()
    app = GuiSimApp(args)
    app.run()

if __name__ == "__main__":
    main()
