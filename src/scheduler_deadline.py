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
