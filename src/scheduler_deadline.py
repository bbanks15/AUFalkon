import csv
from typing import Dict, List, Tuple

class DeadlineScheduler:
    """Deadline-first scheduler (EDF/LLF) with failover pools.

    Enforces hard max_gap_ticks per domain.
    Assigns required_active_per_domain devices per domain each tick.
    Uses domain pools; falls back to spares.

    Notes:
    - This is a PoC control-layer logic simulation; it models assignment decisions and hard invariants.
    - It is deterministic given alive set and mission config.
    """

    def __init__(self, domains: List[str], pools: Dict[str, List[str]], required: int,
                 max_gap_ticks: int, tick_ms: float, capacity_per_unit: int = 2,
                 logs_dir: str = 'logs_deadline'):
        self.domains = domains
        self.pools = pools
        self.required = required
        self.max_gap_ticks = max_gap_ticks
        self.tick_ms = tick_ms
        self.capacity_per_unit = capacity_per_unit

        self.tick = 0
        self.last_service_tick: Dict[str, int] = {d: 0 for d in domains}
        self.prev_assign: Dict[str, List[str]] = {d: [] for d in domains}

        import os
        os.makedirs(logs_dir, exist_ok=True)
        self.timeline_path = f"{logs_dir}/timeline.csv"
        self.matrix_path = f"{logs_dir}/matrix.csv"
        self.timeline_f = open(self.timeline_path, 'w', newline='')
        self.matrix_f = open(self.matrix_path, 'w', newline='')
        self.timeline_w = csv.writer(self.timeline_f)
        self.matrix_w = csv.writer(self.matrix_f)
        self.timeline_w.writerow(['time_ticks','domain','active_devices','reason'])
        self.matrix_w.writerow(['time_ticks'] + [f'domain_{i}_devices' for i in range(len(domains))])

    def close(self):
        try:
            self.timeline_f.close(); self.matrix_f.close()
        except Exception:
            pass

    def _deadline(self, d: str) -> int:
        return self.last_service_tick[d] + self.max_gap_ticks

    def _slack(self, d: str) -> int:
        return self._deadline(d) - self.tick

    def schedule_tick(self, alive: Dict[str, bool]) -> List[Tuple[str, str]]:
        self.tick += 1

        # Order domains by earliest deadline, then least slack
        ordered = sorted(self.domains, key=lambda d: (self._deadline(d), self._slack(d)))

        # Per-unit capacity this tick
        capacity = {u: self.capacity_per_unit for u, ok in alive.items() if ok}

        assignments: List[Tuple[str, str]] = []
        assign_map: Dict[str, List[str]] = {d: [] for d in self.domains}

        spares = self.pools.get('spares', [])

        def candidates_for(domain: str) -> List[str]:
            primary = [u for u in self.pools.get(domain, []) if alive.get(u, False)]
            backup = [u for u in spares if alive.get(u, False)]
            # Domain-first: no fairness; keep deterministic ordering from mission
            return primary + backup

        # Assign required units per domain
        for d in ordered:
            need = self.required
            for u in candidates_for(d):
                if need <= 0:
                    break
                if capacity.get(u, 0) > 0:
                    capacity[u] -= 1
                    assignments.append((d, u))
                    assign_map[d].append(u)
                    self.last_service_tick[d] = self.tick
                    need -= 1
            if need > 0:
                raise RuntimeError(f"INVARIANT FAILURE @tick={self.tick}: DEADLINE_MISS domain={d} need={need}")

        # Verify hard gap
        for d in self.domains:
            gap = self.tick - self.last_service_tick[d]
            if gap > self.max_gap_ticks:
                raise RuntimeError(f"INVARIANT FAILURE @tick={self.tick}: GAP_EXCEEDED domain={d} gap={gap} max={self.max_gap_ticks}")

        # Change-only logging
        changed = False
        for idx, d in enumerate(self.domains):
            prev = self.prev_assign.get(d, [])
            curr = assign_map.get(d, [])
            if prev != curr:
                self.timeline_w.writerow([self.tick, d, ';'.join(curr), 'assignments'])
                changed = True
        if changed:
            row = [self.tick] + [';'.join(assign_map[d]) for d in self.domains]
            self.matrix_w.writerow(row)
        self.prev_assign = {d: assign_map.get(d, [])[:] for d in self.domains}

        return assignments
