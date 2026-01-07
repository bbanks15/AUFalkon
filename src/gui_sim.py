
# src/scheduler_deadline.py
from typing import Dict, List, Set, Tuple

class DeadlineScheduler:
    """
    Deterministic scheduler with:
      - Battery drain per interval, recovery on REST streaks
      - Rotation boundary behavior every 120,000 ticks (2 min)
      - Cooldowns to avoid ping-ponging
      - NORMAL (capacity=1) first, CONTINGENCY (capacity=2) if needed
      - No duplicates within a domain per tick
      - Change-only handoff tracking for GUI narrative
    """

    def __init__(self,
                 domains: List[str],
                 pools: Dict[str, List[str]],
                 required_map: Dict[str, int],
                 max_gap_ticks: int,
                 tick_ms: float,
                 capacity_per_unit: int = 2,
                 logs_dir: str = "",
                 mission: Dict = None):
        # Mission fields
        self.domains = domains[:]
        self.required_map = dict(required_map)
        self.tick_ms = float(tick_ms)
        self.rotation_period_ticks = int((mission or {}).get("rotation_period_ms", 120000) / self.tick_ms)

        # Pools
        self.universal_roles = bool((mission or {}).get("universal_roles", True))
        self.units: List[str] = sorted(list({u for ulist in pools.values() for u in ulist} | set((mission or {}).get("units", []))))
        self.pools = pools

        # Capacity
        self.capacity_per_unit_cfg = int(capacity_per_unit)

        # Battery config
        batt = (mission or {}).get("battery", {})
        self.battery_max = int(batt.get("max", 100000))
        self.battery: Dict[str, int] = {u: int(batt.get("initial", self.battery_max)) for u in self.units}
        self.domain_cost = dict(batt.get("domain_cost", {"radar_ir_gps": 3, "comm_eoir": 2, "network_test_only": 1}))
        self.cost_interval_ticks = int(batt.get("cost_interval_ms", 1000) / self.tick_ms)
        self.rest_cfg = batt.get("rest_recharge", {"every_intervals": 3, "amount": 2000})
        self.cooldown_in_ticks = int(batt.get("cooldown", {}).get("in_ms", 120000) / self.tick_ms)
        self.cooldown_out_ticks = int(batt.get("cooldown", {}).get("out_ms", 120000) / self.tick_ms)

        # State
        self.tick = 0
        self.mode = "NORMAL"
        self.prev_assign: Dict[str, Set[str]] = {d: set() for d in self.domains}
        self.cooldown_in: Dict[str, int] = {u: 0 for u in self.units}
        self.cooldown_out: Dict[str, int] = {u: 0 for u in self.units}
        self.drain_accum: Dict[str, int] = {u: 0 for u in self.units}
        self.rest_intervals: Dict[str, int] = {u: 0 for u in self.units}
        self.active_ticks_in_interval: Dict[str, int] = {u: 0 for u in self.units}

        # Interval snapshot for GUI battery_log
        self.last_interval_drain: Dict[str, int] = {u: 0 for u in self.units}
        self.last_interval_recovery: Dict[str, int] = {u: 0 for u in self.units}

        # Run summary
        self.total_ticks = 0
        self.contingency_ticks = 0
        self.last_mode = "NORMAL"
        self.last_tick_details: Dict = {}

        # Weights (Strong rotation behavior)
        self.W_BATT = 1.0
        self.W_LOAD = 0.5
        self.W_ROTATE_KEEP = 20.0
        self.W_IN_COOLDOWN_STAY_ACTIVE_BONUS = -10.0
        self.W_OUT_COOLDOWN_AVOID_ASSIGN = 10.0
        self.SLOT2_PENALTY = 25.0  # discourage multi-role unless necessary

    # ---------------- core helpers ----------------
    def is_rotation_tick(self, tick: int) -> bool:
        return tick > 0 and (tick % self.rotation_period_ticks == 0)

    def _decrement_cooldowns(self):
        for u in self.units:
            if self.cooldown_in[u] > 0: self.cooldown_in[u] -= 1
            if self.cooldown_out[u] > 0: self.cooldown_out[u] -= 1

    def _unit_capacity(self) -> int:
        return 1 if self.mode == "NORMAL" else self.capacity_per_unit_cfg

    def _allowed_units_for_domain(self, d: str) -> List[str]:
        if self.universal_roles:
            return self.units
        return self.pools.get(d, [])

    def _score(self, u: str, d: str, tick: int, slot_index: int, prev_assign_d: Set[str]) -> float:
        batt_inv = (self.battery_max - self.battery[u])
        score = 0.0
        score += batt_inv * self.W_BATT
        score += self.domain_cost.get(d, 1) * batt_inv * self.W_LOAD
        if self.is_rotation_tick(tick) and u in prev_assign_d:
            score += self.W_ROTATE_KEEP
        if self.cooldown_in[u] > 0 and u in prev_assign_d:
            score += self.W_IN_COOLDOWN_STAY_ACTIVE_BONUS
        if self.cooldown_out[u] > 0 and u not in prev_assign_d:
            score += self.W_OUT_COOLDOWN_AVOID_ASSIGN
        if slot_index > 0:
            score += self.SLOT2_PENALTY
        return score

    def _try_assign(self, tick: int, alive: Dict[str, bool], capacity_limit: int) -> Tuple[bool, Dict[str, List[str]]]:
        roles_per_unit: Dict[str, int] = {u: 0 for u in self.units}
        assign: Dict[str, List[str]] = {d: [] for d in self.domains}

        for d in self.domains:
            prev_d = self.prev_assign.get(d, set())
            candidates = []
            for u in self._allowed_units_for_domain(d):
                if not alive.get(u, True):  # down units excluded
                    continue
                if self.battery[u] <= 0:
                    continue
                slot_index = roles_per_unit[u]
                if slot_index >= capacity_limit:
                    continue
                candidates.append((self._score(u, d, tick, slot_index, prev_d), u))
            candidates.sort(key=lambda t: (t[0], t[1]))  # deterministic by score then id

            # Fill domain requirement
            for _, u in candidates:
                if len(assign[d]) >= self.required_map[d]:
                    break
                if roles_per_unit[u] < capacity_limit and u not in assign[d]:
                    assign[d].append(u)
                    roles_per_unit[u] += 1

            if len(assign[d]) < self.required_map[d]:
                return False, {}

        return True, assign

    # ---------------- public API ----------------
    def schedule_tick(self, alive: Dict[str, bool]) -> List[Tuple[str, str]]:
        """
        Returns list of (domain, unit) assignments for this tick.
        Also updates battery accumulators, cooldowns, and handoff details.
        """
        self.tick += 1
        tick = self.tick

        # Decide mode: try NORMAL (capacity=1) first, else CONTINGENCY (capacity=2)
        self.mode = "NORMAL"
        feasible, assign_map = self._try_assign(tick, alive, capacity_limit=1)
        if not feasible:
            self.mode = "CONTINGENCY"
            feasible, assign_map = self._try_assign(tick, alive, capacity_limit=self.capacity_per_unit_cfg)
            if not feasible:
                raise RuntimeError(f"CRITICAL: infeasible at tick={tick}")

        # Update cooldowns each tick
        self._decrement_cooldowns()

        # Detect rotations to set cooldowns at rotation boundaries
        handoffs: List[Dict] = []
        if self.is_rotation_tick(tick):
            for d in self.domains:
                prev = self.prev_assign.get(d, set())
                now = set(assign_map[d])
                rotated_out = sorted(list(prev - now))
                rotated_in = sorted(list(now - prev))
                for u in rotated_in:
                    self.cooldown_in[u] = self.cooldown_in_ticks
                for u in rotated_out:
                    self.cooldown_out[u] = self.cooldown_out_ticks
                if rotated_in or rotated_out:
                    handoffs.append({
                        "domain": d,
                        "removed": rotated_out,
                        "added": rotated_in,
                        "atomic": True
                    })

        # Save prev_assign for next tick
        self.prev_assign = {d: set(assign_map[d]) for d in self.domains}

        # Accumulate drain and active ticks for interval accounting
        active_units = set()
        for d, units in assign_map.items():
            for u in units:
                self.drain_accum[u] += self.domain_cost.get(d, 1)
                active_units.add(u)
        for u in self.units:
            if u in active_units:
                self.active_ticks_in_interval[u] += 1

        # Interval boundary: apply drain and recovery
        if tick % self.cost_interval_ticks == 0:
            # Capture drain BEFORE reset
            interval_drain = {u: self.drain_accum[u] for u in self.units}
            pre_battery = {u: self.battery[u] for u in self.units}

            # Apply drain
            for u in self.units:
                self.battery[u] = max(0, self.battery[u] - self.drain_accum[u])
                self.drain_accum[u] = 0

            # Recovery for those RESTing the entire interval
            every = int(self.rest_cfg.get("every_intervals", 3))
            amount = int(self.rest_cfg.get("amount", 2000))
            for u in self.units:
                was_rest_full_interval = (self.active_ticks_in_interval.get(u, 0) == 0) and alive.get(u, True)
                if was_rest_full_interval:
                    self.rest_intervals[u] += 1
                    if self.rest_intervals[u] % every == 0:
                        self.battery[u] = min(self.battery_max, self.battery[u] + amount)
                else:
                    self.rest_intervals[u] = 0

            # Compute recovery applied for audit
            post_battery = {u: self.battery[u] for u in self.units}
            interval_recovery = {}
            for u in self.units:
                after_drain = max(0, pre_battery[u] - interval_drain[u])
                interval_recovery[u] = max(0, post_battery[u] - after_drain)

            # Save for GUI log
            self.last_interval_drain = interval_drain
            self.last_interval_recovery = interval_recovery

            # Reset active tick counters for next interval
            self.active_ticks_in_interval = {u: 0 for u in self.units}

        # Update run summary
        self.total_ticks += 1
        if self.mode == "CONTINGENCY":
            self.contingency_ticks += 1
        self.last_mode = self.mode

        # Build flat assignments list for GUI
        assignments = []
        for d in self.domains:
            for u in assign_map[d]:
                assignments.append((d, u))

        # Save tick details for GUI
        self.last_tick_details = {
            "handoffs": handoffs,
            "assign_map": {d: list(assign_map[d]) for d in self.domains}
        }
        return assignments

    def get_last_tick_details(self) -> Dict:
        return dict(self.last_tick_details)

    def get_run_summary(self) -> Dict:
        return {
            "contingency_ticks": self.contingency_ticks,
            "total_ticks": self.total_ticks,
            "last_mode": self.last_mode
        }

    def close(self):
        # No external resources to release, but method kept for API parity
        pass
