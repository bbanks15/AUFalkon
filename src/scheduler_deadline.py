
# src/scheduler_deadline.py
from collections import defaultdict
from typing import Dict, List, Set, Tuple

class SchedulerState:
    """
    Deterministic scheduler with:
      - Battery drain per cost interval (1000 ticks), recovery in REST (+2 per 3 intervals)
      - Rotation boundary behavior every 120,000 ticks (2 minutes)
      - Cooldowns to avoid ping-ponging after rotation
      - NORMAL (capacity=1) first, CONTINGENCY (capacity=2) if needed
      - No duplicates within a domain per tick
    """

    def __init__(self, mission: Dict):
        self.tick_ms = mission.get("tick_ms", 1)
        self.rotation_period_ticks = mission.get("rotation_period_ms", 120000) // self.tick_ms

        batt_cfg = mission["battery"]
        self.battery_max = batt_cfg["max"]
        self.cost_interval_ticks = batt_cfg["cost_interval_ms"] // self.tick_ms
        self.domain_cost = dict(batt_cfg["domain_cost"])

        self.cooldown_in_ticks = batt_cfg["cooldown"]["in_ms"] // self.tick_ms
        self.cooldown_out_ticks = batt_cfg["cooldown"]["out_ms"] // self.tick_ms

        self.units: List[str] = mission["units"]
        self.required_active: Dict[str, int] = mission["required_active"]
        # Prefer mission.domains if provided; else derive from required_active keys
        self.domains: List[str] = mission.get("domains") or list(self.required_active.keys())

        # Battery state
        self.battery: Dict[str, int] = {u: batt_cfg["initial"] for u in self.units}
        self.drain_accum: Dict[str, int] = {u: 0 for u in self.units}
        self.rest_intervals: Dict[str, int] = {u: 0 for u in self.units}

        # Cooldowns (ticks remaining)
        self.cooldown_in: Dict[str, int] = {u: 0 for u in self.units}
        self.cooldown_out: Dict[str, int] = {u: 0 for u in self.units}

        # Previous assignment for rotation logic
        self.prev_assign: Dict[str, Set[str]] = {d: set() for d in self.domains}

        # Mode tracking
        self.mode: str = "NORMAL"

        # Weights (Strong rotation behavior)
        self.W_BATT = 1.0
        self.W_LOAD = 0.5
        self.W_ROTATE_KEEP = 20.0
        self.W_IN_COOLDOWN_STAY_ACTIVE_BONUS = -10.0
        self.W_OUT_COOLDOWN_AVOID_ASSIGN = 10.0
        self.SLOT2_PENALTY = 25.0  # discourage multi-role unless necessary

    def is_rotation_tick(self, tick: int) -> bool:
        return tick > 0 and (tick % self.rotation_period_ticks == 0)

    def decrement_cooldowns(self):
        for u in self.units:
            if self.cooldown_in[u] > 0:
                self.cooldown_in[u] -= 1
            if self.cooldown_out[u] > 0:
                self.cooldown_out[u] -= 1

    def compute_unit_capacity(self) -> int:
        return 1 if self.mode == "NORMAL" else 2

    def score(self, u: str, d: str, tick: int, slot_index: int, prev_assign_d: Set[str]) -> float:
        batt_inv = (self.battery_max - self.battery[u])
        score = 0.0
        # Prefer higher battery (lower inverse)
        score += batt_inv * self.W_BATT
        # Prefer high-battery for high-cost domains
        score += self.domain_cost[d] * batt_inv * self.W_LOAD

        # Rotation boundary: penalize keeping same mapping
        if self.is_rotation_tick(tick) and u in prev_assign_d:
            score += self.W_ROTATE_KEEP

        # Cooldowns
        if self.cooldown_in[u] > 0 and u in prev_assign_d:
            score += self.W_IN_COOLDOWN_STAY_ACTIVE_BONUS
        if self.cooldown_out[u] > 0 and u not in prev_assign_d:
            score += self.W_OUT_COOLDOWN_AVOID_ASSIGN

        # Slot2 penalty
        if slot_index > 0:
            score += self.SLOT2_PENALTY

        return score

    def try_assign(self, tick: int, capacity_limit: int) -> Tuple[bool, Dict[str, List[str]]]:
        roles_per_unit: Dict[str, int] = {u: 0 for u in self.units}
        assign: Dict[str, List[str]] = {d: [] for d in self.domains}

        for d in self.domains:
            prev_d = self.prev_assign.get(d, set())
            candidates = []
            for u in self.units:
                # Cannot be assigned twice in same domain
                if u in assign[d]:
                    continue
                # Zero battery cannot be assigned
                if self.battery[u] <= 0:
                    continue
                slot_index = roles_per_unit[u]
                if slot_index >= capacity_limit:
                    continue
                s = self.score(u, d, tick, slot_index, prev_d)
                candidates.append((s, u))
            candidates.sort(key=lambda t: (t[0], t[1]))  # deterministic by score then id

            # Fill domain requirement
            for _, u in candidates:
                if len(assign[d]) >= self.required_active[d]:
                    break
                if roles_per_unit[u] < capacity_limit and u not in assign[d]:
                    assign[d].append(u)
                    roles_per_unit[u] += 1

            if len(assign[d]) < self.required_active[d]:
                return False, {}

        return True, assign

    def step(self, tick: int) -> Dict[str, List[str]]:
        # Try NORMAL first
        self.mode = "NORMAL"
        feasible, assign = self.try_assign(tick, capacity_limit=1)
        if not feasible:
            # Fallback to CONTINGENCY (capacity=2)
            self.mode = "CONTINGENCY"
            feasible, assign = self.try_assign(tick, capacity_limit=2)
            if not feasible:
                raise RuntimeError(f"CRITICAL: infeasible at tick={tick}")

        # Update cooldowns
        self.decrement_cooldowns()

        # Set cooldowns at rotation boundaries based on changes
        if self.is_rotation_tick(tick):
            for d in self.domains:
                prev = self.prev_assign.get(d, set())
                now = set(assign[d])
                rotated_out = prev - now
                rotated_in = now - prev
                for u in rotated_in:
                    self.cooldown_in[u] = self.cooldown_in_ticks
                for u in rotated_out:
                    self.cooldown_out[u] = self.cooldown_out_ticks

        # Save prev_assign for next tick
        self.prev_assign = {d: set(assign[d]) for d in self.domains}
        return assign

    def accumulate_drain(self, assign: Dict[str, List[str]]) -> Set[str]:
        active_units = set()
        for d, units in assign.items():
            for u in units:
                self.drain_accum[u] += self.domain_cost[d]
                active_units.add(u)
        return active_units

    def update_battery_interval(self, active_ticks_map: Dict[str, int], rest_cfg: Dict[str, int]):
        """
        Apply battery drain once per interval and handle recovery if unit rested the whole interval.
        Drain is applied from self.drain_accum[u] then reset to 0.
        Recovery (+amount every 'every_intervals') only if unit rested ENTIRE interval.
        """
        for u in self.units:
            # Apply drain (clamped)
            self.battery[u] = max(0, self.battery[u] - self.drain_accum[u])
            self.drain_accum[u] = 0

            # Rest & recovery
            was_rest_full_interval = (active_ticks_map.get(u, 0) == 0)
            if was_rest_full_interval:
                self.rest_intervals[u] += 1
                every = rest_cfg["every_intervals"]
                amount = rest_cfg["amount"]
                if self.rest_intervals[u] % every == 0:
                    self.battery[u] = min(self.battery_max, self.battery[u] + amount)
            else:
                self.rest_intervals[u] = 0
