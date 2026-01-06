import json, argparse
from scheduler_deadline import DeadlineScheduler

class FailureTimeline:
    def __init__(self, units, initial_faults=0):
        self.units = units[:]
        self.alive = {u: True for u in units}
        self.recover_at = {u: None for u in units}
        self.permanent = {u: False for u in units}

        # Apply initial permanent faults to the first N units (deterministic fault sweep)
        n = max(0, min(int(initial_faults), len(units)))
        for u in units[:n]:
            self.alive[u] = False
            self.permanent[u] = True
            self.recover_at[u] = None

    def apply_events(self, events, tick_ms, current_tick):
        now_ms = int(current_tick * tick_ms)

        # Apply events that start now
        for ev in events:
            start = int(ev.get('at_ms', 0))
            dur = int(ev.get('duration_ms', 0))
            unit = ev.get('unit')
            typ = ev.get('type')
            permanent = bool(ev.get('permanent', False))
            if unit is None:
                continue
            if start == now_ms:
                if typ in ('unit_crash', 'effector_failure', 'auth_fail'):
                    self.alive[unit] = False
                    if permanent:
                        self.permanent[unit] = True
                        self.recover_at[unit] = None
                    else:
                        self.recover_at[unit] = start + dur if dur > 0 else None

        # Handle recoveries for temporary failures
        for u in list(self.alive.keys()):
            ra = self.recover_at.get(u)
            if ra is not None and now_ms >= ra and not self.permanent.get(u, False):
                self.alive[u] = True
                self.recover_at[u] = None

    def status(self):
        return dict(self.alive)


def run_mission(mission_path: str, ticks: int, logs_dir: str, initial_faults: int = 0, capacity_per_unit: int = 2):
    with open(mission_path, 'r', encoding='utf-8') as f:
        mission = json.load(f)

    tick_ms = float(mission.get('tick_ms', 1.0))
    max_gap_ms = int(mission['constraints']['max_gap_ms'])
    max_gap_ticks = max(1, int(max_gap_ms / tick_ms))

    required = int(mission['required_active_per_domain'])
    domains = mission['domains']

    pools = {d: mission['domain_pools'].get(d, []) for d in domains}
    pools['spares'] = mission['domain_pools'].get('spares', [])

    sched = DeadlineScheduler(domains, pools, required, max_gap_ticks, tick_ms,
                              capacity_per_unit=capacity_per_unit,
                              logs_dir=logs_dir)

    ft = FailureTimeline(mission['units'], initial_faults=initial_faults)
    events = mission.get('failure_injections', [])

    status = 'PASS'
    error = ''
    try:
        for _ in range(1, ticks + 1):
            ft.apply_events(events, tick_ms, sched.tick)
            sched.schedule_tick(ft.status())
    except Exception as e:
        status = 'FAIL'
        error = str(e)
    finally:
        sched.close()

    return {'status': status, 'error': error, 'logs_dir': logs_dir, 'initial_faults': int(initial_faults)}


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('mission')
    ap.add_argument('--ticks', type=int, default=200)
    ap.add_argument('--logs_dir', default='runner_logs')
    ap.add_argument('--initial_faults', type=int, default=0)
    ap.add_argument('--capacity_per_unit', type=int, default=2)
    args = ap.parse_args()

    result = run_mission(args.mission, args.ticks, args.logs_dir, args.initial_faults, args.capacity_per_unit)
    print(result)
