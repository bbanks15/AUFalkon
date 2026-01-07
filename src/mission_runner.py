
import json, argparse, os, csv
from datetime import datetime, timedelta
from scheduler_deadline import DeadlineScheduler


class FailureTimeline:
    def __init__(self, units, initial_faults=0):
        self.units = units[:]
        self.alive = {u: True for u in units}
        self.recover_at = {u: None for u in units}
        self.permanent = {u: False for u in units}

        # Deterministic initial permanent faults: first N units
        n = max(0, min(int(initial_faults), len(units)))
        for u in units[:n]:
            self.alive[u] = False
            self.permanent[u] = True
            self.recover_at[u] = None

    def apply_events(self, events, tick_ms, current_tick):
        """
        Returns a list of fault events applied this tick:
          dict(type=DOWN/UP, unit=..., permanent=bool, reason=..., at_ms=...)
        """
        now_ms = int(current_tick * tick_ms)
        emitted = []

        # Apply events that start now
        for ev in events:
            start = int(ev.get("at_ms", 0))
            dur = int(ev.get("duration_ms", 0))
            unit = ev.get("unit")
            typ = ev.get("type")
            permanent = bool(ev.get("permanent", False))
            if unit is None:
                continue

            if start == now_ms:
                if typ in ("unit_crash", "effector_failure", "auth_fail"):
                    self.alive[unit] = False
                    if permanent:
                        self.permanent[unit] = True
                        self.recover_at[unit] = None
                    else:
                        self.recover_at[unit] = start + dur if dur > 0 else None

                    emitted.append({
                        "type": "DOWN",
                        "unit": unit,
                        "permanent": permanent,
                        "reason": typ,
                        "at_ms": now_ms
                    })

        # Handle recoveries for temporary failures
        for u in list(self.alive.keys()):
            ra = self.recover_at.get(u)
            if ra is not None and now_ms >= ra and not self.permanent.get(u, False):
                self.alive[u] = True
                self.recover_at[u] = None
                emitted.append({
                    "type": "UP",
                    "unit": u,
                    "permanent": False,
                    "reason": "recovery",
                    "at_ms": now_ms
                })

        return emitted

    def status(self):
        return dict(self.alive)


def _required_map(mission, domains):
    cfg = mission.get("required_active_per_domain", 1)
    if isinstance(cfg, dict):
        rm = {d: int(cfg.get(d, 1)) for d in domains}
    else:
        rm = {d: int(cfg) for d in domains}
    for d, v in rm.items():
        if v <= 0:
            raise ValueError(f"required_active_per_domain for '{d}' must be > 0, got {v}")
    return rm


from datetime import datetime, timedelta

class EventStream:
    """
    Human-readable, change-only event stream.

    Uses SIMULATED wall time:
      Start epoch = 01-01-26 00:00:00.000
      wall_time(tick) = epoch + (tick * tick_ms)

    Writes lines like:
      [tick=120 sim_ms=120 wall=01-01-26 00:00:00.120] Device D entered FAULTED state
    """
    def __init__(self, path: str, tick_ms: float, include_wall_time: bool = True):
        self.path = path
        self.tick_ms = float(tick_ms)
        self.include_wall_time = include_wall_time

        # Fixed simulation epoch (naive datetime, deterministic)
        self.epoch = datetime(2026, 1, 1, 0, 0, 0, 0)

        self.f = open(path, "w", encoding="utf-8")
        self.f.write("# event_stream.log (change-only)\n")
        self.f.write("# format: [tick=... sim_ms=... wall=MM-DD-YY HH:MM:SS.mmm] message\n")
        self.f.write("# wall time is SIMULATED: epoch(01-01-26 00:00:00.000) + sim_ms\n")

        self.prev_alive = None  # type: ignore
        self.prev_roles = {}    # unit -> set(domains)
        self.prev_rest = set()

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass

    def _fmt_wall(self, sim_ms: int) -> str:
        dt = self.epoch + timedelta(milliseconds=sim_ms)
        # Ensure millisecond precision (mmm)
        ms = dt.microsecond // 1000
        return f"{dt.strftime('%m-%d-%y %H:%M:%S')}.{ms:03d}"

    def _stamp(self, tick: int) -> str:
        sim_ms = int(round(tick * self.tick_ms))
        if not self.include_wall_time:
            return f"[tick={tick} sim_ms={sim_ms}]"
        return f"[tick={tick} sim_ms={sim_ms} wall={self._fmt_wall(sim_ms)}]"

    def emit(self, tick: int, msg: str):
        self.f.write(f"{self._stamp(tick)} {msg}\n")
        self.f.flush()

    def update(self, tick: int, alive: dict, assignments: list, domains: list):
        """
        Compare state to previous tick and emit change-only narrative events.
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

        # Save for next tick
        self.prev_alive = dict(alive)
        self.prev_roles = {u: set(roles.get(u, set())) for u in alive.keys()}
        self.prev_rest = set(rest_units)


def run_mission(mission_path: str, ticks: int, logs_dir: str, initial_faults: int = 0, capacity_per_unit: int = 2):
    with open(mission_path, "r", encoding="utf-8") as f:
        mission = json.load(f)

    tick_ms = float(mission.get("tick_ms", 1.0))
    max_gap_ms = int(mission["constraints"]["max_gap_ms"])
    max_gap_ticks = max(1, int(max_gap_ms / tick_ms))

    domains = mission["domains"]
    required_map = _required_map(mission, domains)

    # universal roles => all units eligible for all domains
    universal = bool(mission.get("universal_roles", False))
    if universal:
        pools = {d: mission["units"][:] for d in domains}
        pools["spares"] = []
    else:
        pools = {d: mission["domain_pools"].get(d, []) for d in domains}
        pools["spares"] = mission["domain_pools"].get("spares", [])

    os.makedirs(logs_dir, exist_ok=True)

    # Fault event log (device down/up over time)
    fault_log_path = os.path.join(logs_dir, "fault_events.csv")
    fault_f = open(fault_log_path, "w", newline="", encoding="utf-8")
    fault_w = csv.writer(fault_f)
    fault_w.writerow(["tick", "sim_ms", "event", "unit", "permanent", "reason"])

    # Human narrative event stream
    evs = EventStream(os.path.join(logs_dir, "event_stream.log"), tick_ms=tick_ms, include_wall_time=True)

    # Log initial sweep faults at tick=0
    units = mission["units"]
    n = max(0, min(int(initial_faults), len(units)))
    for u in units[:n]:
        fault_w.writerow([0, 0, "DOWN", u, "YES", "initial_faults"])
    fault_f.flush()

    sched = DeadlineScheduler(
        domains, pools, required_map, max_gap_ticks, tick_ms,
        capacity_per_unit=capacity_per_unit,
        logs_dir=logs_dir
    )

    ft = FailureTimeline(mission["units"], initial_faults=initial_faults)
    events = mission.get("failure_injections", [])

    status = "PASS"
    error = ""
    run_summary = {}

    # Seed event stream baseline at tick=0 (so first real tick can show transitions)
    try:
        evs.prev_alive = ft.status()
        evs.prev_roles = {u: set() for u in ft.status().keys()}
        evs.prev_rest = set(u for u, ok in ft.status().items() if ok)
    except Exception:
        pass

    try:
        for _ in range(1, ticks + 1):
            # apply time-based events at current sched.tick (before scheduling)
            emitted = ft.apply_events(events, tick_ms, sched.tick)
            if emitted:
                for ev in emitted:
                    fault_w.writerow([
                        sched.tick,
                        int(sched.tick * tick_ms),
                        ev["type"],
                        ev["unit"],
                        "YES" if ev.get("permanent", False) else "NO",
                        ev.get("reason", "")
                    ])
                fault_f.flush()

            alive = ft.status()
            assignments = sched.schedule_tick(alive)

            # Update narrative stream after scheduling decisions (captures "who filled in")
            evs.update(sched.tick, alive, assignments, domains)

        if hasattr(sched, "get_run_summary"):
            run_summary = sched.get_run_summary()

    except Exception as e:
        status = "FAIL"
        error = str(e)
        if hasattr(sched, "get_run_summary"):
            run_summary = sched.get_run_summary()
    finally:
        try:
            fault_f.close()
        except Exception:
            pass
        try:
            evs.close()
        except Exception:
            pass
        sched.close()

    # Write summary file
    try:
        with open(os.path.join(logs_dir, "run_summary.json"), "w", encoding="utf-8") as f:
            json.dump(run_summary, f, indent=2)
    except Exception:
        pass

    return {
        "status": status,
        "error": error,
        "logs_dir": logs_dir,
        "initial_faults": int(initial_faults),
        "run_summary": run_summary
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mission")
    ap.add_argument("--ticks", type=int, default=200)
    ap.add_argument("--logs_dir", default="runner_logs")
    ap.add_argument("--initial_faults", type=int, default=0)
    ap.add_argument("--capacity_per_unit", type=int, default=2)
    args = ap.parse_args()

    result = run_mission(args.mission, args.ticks, args.logs_dir, args.initial_faults, args.capacity_per_unit)
    print(json.dumps(result))
