
# src/scheduler_deadline.py
# DeadlineScheduler with rotation/cooldown weights, hysteresis, and drain breakdown
from dataclasses import dataclass, field
import math
import time

class DeadlineScheduler:
    def __init__(self, mission, *, tick_ms=1000,
                 rotation_weight=0.40, cooldown_weight=0.60,
                 min_dwell_ticks=30, hysteresis_pct=0.08,
                 battery_reserve_pct=0.15,
                 capacity_per_unit=2):
        """
        mission: dict-like structure (parsed JSON)
        """
        self.mission = mission
        self.tick_ms = tick_ms

        # Tuning
        self.rotation_weight = rotation_weight
        self.cooldown_weight = cooldown_weight
        self.min_dwell_ticks = min_dwell_ticks
        self.hysteresis_pct = hysteresis_pct
        self.battery_reserve_pct = battery_reserve_pct

        # Capacity/units
        self.capacity_per_unit = capacity_per_unit
        self.units = self._init_units(mission)
        self._active_unit_id = self.units[0]["id"] if self.units else None

        # Battery base = 100k (per previous chat)
        self.battery_max = 100000.0

        # Cooldowns from mission (ms)
        self.cooldown_in_ms = self._get_mission_value(mission, ["cooldown_in_ms"], default=120000)
        self.cooldown_out_ms = self._get_mission_value(mission, ["cooldown_out_ms"], default=120000)
        self.rotation_period_ms = self._get_mission_value(mission, ["rotation_period_ms"], default=120000)

        # Internal cooldown tracking per unit
        self.cooldowns = {u["id"]: {"in": 0, "out": 0} for u in self.units}

        # Per-tick bookkeeping
        self._rotation_deadline_ms = self.rotation_period_ms
        self._elapsed_since_rotation_ms = 0

        # Drain accounting
        self.last_interval_drain = 0.0
        self.last_interval_recovery = 0.0
        self.last_interval_drain_breakdown = {}

        # Domain costs for breakdown
        self.domain_costs = mission.get("domain_costs", {"radar":3, "comm":2, "network":1})
        self.domains = list(self.domain_costs.keys())

        # Handoff logging hook (file writing is in GUI; here track reason)
        self._last_handoff_reason = None

    # ---------- Initialization helpers ----------
    def _get_mission_value(self, mission, keys, default=None):
        for k in keys:
            if isinstance(mission, dict) and k in mission:
                return mission[k]
        return default

    def _init_units(self, mission):
        # Expect mission["units"] list of dicts with "id", optional "capabilities"
        units = mission.get("units")
        if isinstance(units, list) and units:
            result = []
            for i, u in enumerate(units):
                uid = u.get("id") or u.get("name") or f"unit{i+1}"
                result.append({
                    "id": uid,
                    "battery": self.battery_max,
                    "battery_pct": 1.0,
                    "dwell_ticks": 0,
                    "capabilities": u.get("capabilities", None)
                })
            return result
        # Fallback: synthesize 3 units
        return [{"id": f"unit{i+1}", "battery": self.battery_max, "battery_pct": 1.0, "dwell_ticks": 0, "capabilities": None} for i in range(3)]

    # ---------- Public getters ----------
    def get_unit_ids(self):
        return [u["id"] for u in self.units]

    def current_unit_id(self):
        return self._active_unit_id

    def get_unit_battery_pct(self, unit_id):
        for u in self.units:
            if u["id"] == unit_id:
                return u["battery"] / self.battery_max
        return 0.0

    def rotation_remaining_ms(self):
        return max(0, self.rotation_period_ms - self._elapsed_since_rotation_ms)

    # ---------- Step ----------
    def step(self):
        """Advance one tick: cooldowns, battery drain/recovery, rotation."""
        # Update elapsed
        self._elapsed_since_rotation_ms += self.tick_ms

        # Update cooldown counters
        for uid in self.cooldowns:
            for k in ("in", "out"):
                self.cooldowns[uid][k] = max(0, self.cooldowns[uid][k] - self.tick_ms)

        # Battery drain/recovery for this tick
        active = self._active_unit_id
        drain = self._compute_interval_drain(active)
        self.last_interval_drain = drain

        # Distribute drain across domains for GUI badges (proportional to costs)
        wsum = sum(self.domain_costs.values()) or 1
        self.last_interval_drain_breakdown = {d: drain * (self.domain_costs[d] / wsum) for d in self.domains}

        self._apply_battery_change(active, -drain)

        # Recovery: resting units get +2% per 3 intervals rest (as earlier)
        recovery = 0.0
        for u in self.units:
            if u["id"] != active:
                # accrue per 3 ticks → 2% of max
                if (u["dwell_ticks"] % 3) == 0:
                    rec = 0.02 * self.battery_max
                    self._apply_battery_change(u["id"], rec)
                    recovery += rec
        self.last_interval_recovery = recovery

        # Rotation decision if rotation period elapsed or reserve breached
        if (self._elapsed_since_rotation_ms >= self.rotation_period_ms) or (self.get_unit_battery_pct(active) <= self.battery_reserve_pct):
            next_uid = self._pick_next_unit(active)
            if next_uid != active:
                self._handoff_to(next_uid, reason="weighted_rotation")
                self._elapsed_since_rotation_ms = 0

        # Increment dwell ticks
        for u in self.units:
            if u["id"] == self._active_unit_id:
                u["dwell_ticks"] = u.get("dwell_ticks", 0) + 1
            else:
                u["dwell_ticks"] = u.get("dwell_ticks", 0)

        # Coverage update can be done in GUI; scheduler stays compatible.

    # ---------- Battery helpers ----------
    def _apply_battery_change(self, unit_id, delta):
        for u in self.units:
            if u["id"] == unit_id:
                u["battery"] = max(0.0, min(self.battery_max, u["battery"] + delta))
                u["battery_pct"] = u["battery"] / self.battery_max
                return

    def _compute_interval_drain(self, active):
        """
        Base drain per 1000 ticks; scale by tick_ms. If no active unit, drain=0.
        """
        if active is None:
            return 0.0
        # Example: 100 units per 1000 ms; scale to tick
        base_per_sec = 100.0
        drain = base_per_sec * (self.tick_ms / 1000.0)
        return drain

    # ---------- Rotation scoring ----------
    def _cooldown_penalty(self, unit_id):
        cd = self.cooldowns.get(unit_id, {"in":0, "out":0})
        rem_in = cd["in"]
        rem_out = cd["out"]
        tot = max(self.cooldown_in_ms, 1) + max(self.cooldown_out_ms, 1)
        rem = rem_in + rem_out
        frac = 0.0 if tot <= 0 else rem / tot
        return (frac ** 2) * self.cooldown_weight

    def _feasible_state(self, unit_id):
        """
        Placeholder mapping to NORMAL/CONTINGENCY/NO based on capabilities or mission hints.
        For compatibility: assume NORMAL unless mission flags otherwise.
        """
        return "NORMAL"

    def _rotation_score(self, candidate, active):
        cand_batt = self.get_unit_battery_pct(candidate)
        act_batt = self.get_unit_battery_pct(active)
        batt_gain = max(0.0, cand_batt - act_batt)

        cd_pen = self._cooldown_penalty(candidate)

        feas = self._feasible_state(candidate)
        if feas == "NORMAL":
            domain_bonus = 0.20
        elif feas == "CONTINGENCY":
            domain_bonus = 0.08
        else:
            domain_bonus = -0.50

        score = (self.rotation_weight * batt_gain) + domain_bonus - cd_pen
        return score

    def _eligible_to_rotate(self, active):
        # Minimum dwell requirement OR if under reserve
        dwell = 0
        for u in self.units:
            if u["id"] == active:
                dwell = u.get("dwell_ticks", 0)
                break
        if dwell < self.min_dwell_ticks and self.get_unit_battery_pct(active) > self.battery_reserve_pct:
            return False
        return True

    def _pick_next_unit(self, active):
        # Compare candidates; apply hysteresis against 'stay' score
        stay_score = 0.0 - self._cooldown_penalty(active)

        best = active
        best_score = stay_score
        for uid in self.get_unit_ids():
            if uid == active:
                continue
            s = self._rotation_score(uid, active)
            if s > best_score:
                best = uid
                best_score = s

        improved = (best_score - stay_score) > (self.hysteresis_pct)
        if best != active and self._eligible_to_rotate(active) and improved:
            return best
        return active

    def _handoff_to(self, next_uid, reason="rotation"):
        prev = self._active_unit_id
        # set cooldowns (in/out)
        if prev:
            self.cooldowns[prev]["out"] = self.cooldown_out_ms
        self.cooldowns[next_uid]["in"] = self.cooldown_in_ms

        self._active_unit_id = next_uid
        self._last_handoff_reason = reason
        # File logging done in GUI; this keeps scheduler clean.
