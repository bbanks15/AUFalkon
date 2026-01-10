
"""
scheduler_deadline.py

Lean logging + reporting-friendly DeadlineScheduler.

What’s logged (low bloat, high signal):
- timeline.csv                (only when assignments change)
- battery_samples.csv         (every sample_every_ticks; per-unit rows)
- assignment_samples.csv      (every sample_every_ticks; one row per sample)
- events.csv                  (only when events occur)
- summary.json                (written on close())

Core behavior:
- Per-domain requirements (required_map)
- Hard max-gap enforcement
- Universal pool mode
- Weighted battery drain per domain (domain_weights)
- Battery==0 => PERMANENT DEAD (never recharges, never assignable)
- Wake hysteresis + stickiness to reduce churn
- Distinctness enforcement:
  If enough assignable units exist, prefer distinct devices over multi-role
- Atomic rotation boundary (rotation_period_ms)
- Optional per-(unit,domain) temporary faults
"""

import csv
import json
import os
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional, Set


@dataclass
class ScheduleEvent:
    tick: int
    time_ms: int
    kind: str
    detail: str


class DeadlineScheduler:
    DEFAULT_BATTERY_LIFE_MS = 7 * 60 * 1000  # 420000 ms

    def __init__(
        self,
        domains: List[str],
        pools: Dict[str, List[str]],
        required_map: Dict[str, int],
        max_gap_ticks: int,
        tick_ms: float,
        capacity_per_unit: int = 2,
        logs_dir: str = "logs_deadline",

        universal_roles: bool = True,

        battery_life_ms: int = DEFAULT_BATTERY_LIFE_MS,
        swap_threshold_pct: float = 10.0,
        battery_reserve_pct: float = 0.15,
        hysteresis_pct: float = 0.08,
        wake_threshold_pct: Optional[float] = None,

        domain_weights: Optional[Dict[str, float]] = None,

        rotation_period_ms: int = 120000,
        min_dwell_ticks: int = 30,
        rotation_weight: float = 0.40,
        cooldown_weight: float = 0.60,
        keep_bonus: float = 0.35,


        # Low-battery event logging controls (optional)
        #  - every_ms = 0 (default) preserves current behavior (emit every tick while <= threshold)
        #  - every_ms > 0 throttles low_battery_active to at most once per unit per interval
        low_battery_event_every_ms: int = 0,
        low_battery_event_crossing_only: bool = False,

        # Mission failure controls (optional)
        #  - strict=False preserves current behavior (continue sim even if unmet requirements)
        #  - strict=True raises RuntimeError after max_gap_ticks of unmet requirements
        strict_mission_failure: bool = True,

        # Logging controls
        sample_every_ticks: int = 50,
    ):
        # --- Static configuration ---
        # Domains
        self.domains = list(domains)  # report/UI domains (may include 'rest')
        # Treat a domain named 'rest' (case-insensitive) as a special reporting domain.
        # - It is NOT scheduled like other domains (no required_map, no gap enforcement).
        # - Its domain_weight scales recharge while resting.
        self.rest_domain = None
        for _d in self.domains:
            if str(_d).lower() == 'rest':
                self.rest_domain = _d
                break
        self.domains_active = [d for d in self.domains if d != self.rest_domain]

        self.pools = dict(pools or {})
        _rm = dict(required_map or {})
        if getattr(self, 'rest_domain', None) is not None and self.rest_domain in _rm:
            _rm.pop(self.rest_domain, None)
        self.required_map = _rm
        self.max_gap_ticks = int(max_gap_ticks)
        self.tick_ms = float(tick_ms)
        self.capacity_per_unit = int(capacity_per_unit)
        self.universal_roles = bool(universal_roles)

        # --- Battery knobs ---
        self.battery_life_ms = int(battery_life_ms)
        self.swap_threshold_pct = float(swap_threshold_pct)
        self.battery_reserve_pct = float(battery_reserve_pct)
        self.hysteresis_pct = float(hysteresis_pct)
        self._wake_threshold_pct_override = None if wake_threshold_pct is None else float(wake_threshold_pct)

        # --- Low-battery event throttling (optional; defaults preserve current behavior) ---
        self.low_battery_event_every_ms = int(low_battery_event_every_ms)
        self.low_battery_event_crossing_only = bool(low_battery_event_crossing_only)
        if self.low_battery_event_every_ms > 0:
            denom = max(0.0001, float(self.tick_ms))
            self.low_battery_event_every_ticks = max(1, int(round(self.low_battery_event_every_ms / denom)))
        else:
            self.low_battery_event_every_ticks = 0
        self._last_low_battery_warn_tick: Dict[str, int] = {}

        # --- Mission failure gating (optional) ---
        self.strict_mission_failure = bool(strict_mission_failure)
        self._unmet_requirements_streak = 0

        # --- Domain weights ---
        self.domain_weights: Dict[str, float] = {d: 1.0 for d in self.domains}
        if isinstance(domain_weights, dict):
            for d in self.domains:
                if d in domain_weights:
                    try:
                        self.domain_weights[d] = float(domain_weights[d])
                    except Exception:
                        self.domain_weights[d] = 1.0

        # --- Rotation/stability knobs ---
        self.rotation_period_ms = int(rotation_period_ms)
        self.min_dwell_ticks = int(min_dwell_ticks)
        self.rotation_weight = float(rotation_weight)
        self.cooldown_weight = float(cooldown_weight)
        self.keep_bonus = float(keep_bonus)

        # --- Sampling ---
        self.sample_every_ticks = max(1, int(sample_every_ticks))

        # --- Time state ---
        self.tick = 0
        self._closed = False  # set True after close(); prevents writes to closed files
        self.last_service_tick: Dict[str, int] = {d: 0 for d in self.domains_active}
        # --- Assignment memory ---
        self.prev_assign: Dict[str, List[str]] = {d: [] for d in self.domains}
        self.last_assign_map: Dict[str, List[str]] = {d: [] for d in self.domains}

        # --- Outputs for UI ---
        self.rest_units: Set[str] = set()
        self.events: List[ScheduleEvent] = []

        # --- Battery state ---
        self.battery_pct: Dict[str, float] = {}
        self.battery_dead: Set[str] = set()

        # --- Faults ---
        self._domain_faults: Dict[Tuple[str, str], Optional[int]] = {}

        # --- Rotation bookkeeping ---
        self._last_rotation_ms = 0

        # --- Cooldown/dwell bookkeeping ---
        self._last_assigned_tick: Dict[str, int] = {}
        self._active_since_tick: Dict[str, int] = {}

        # --- Rest bookkeeping (wake hysteresis gating) ---
        self._resting_since_tick: Dict[str, int] = {}

        # --- Summary counters (for summary.json) ---
        self._total_roles_required = self._total_required_roles()
        self._ticks_total = 0
        self._ticks_distinct_ok = 0
        self._ticks_multi_role = 0
        self._total_assignments = 0
        self._battery_dead_first_tick: Dict[str, int] = {}

        # --- Logging setup ---
        os.makedirs(logs_dir, exist_ok=True)
        self.logs_dir = logs_dir

        self.timeline_path = os.path.join(logs_dir, "timeline.csv")
        self.battery_samples_path = os.path.join(logs_dir, "battery_samples.csv")
        self.assignment_samples_path = os.path.join(logs_dir, "assignment_samples.csv")
        self.events_path = os.path.join(logs_dir, "events.csv")
        self.summary_path = os.path.join(logs_dir, "summary.json")

        self.timeline_f = open(self.timeline_path, "w", newline="", encoding="utf-8")
        self.battery_f = open(self.battery_samples_path, "w", newline="", encoding="utf-8")
        self.assign_f = open(self.assignment_samples_path, "w", newline="", encoding="utf-8")
        self.events_f = open(self.events_path, "w", newline="", encoding="utf-8")

        self.timeline_w = csv.writer(self.timeline_f)
        self.battery_w = csv.writer(self.battery_f)
        self.assign_w = csv.writer(self.assign_f)
        self.events_w = csv.writer(self.events_f)

        self.timeline_w.writerow(["time_ticks", "time_ms", "domain", "active_devices", "reason"])
        self.battery_w.writerow(["sample_tick", "time_ms", "unit", "battery_pct", "state"])
        self.assign_w.writerow(["sample_tick", "time_ms", "desired_distinct", "actual_distinct"] + [f"domain_{d}_devices" for d in self.domains])
        self.events_w.writerow(["time_ticks", "time_ms", "kind", "detail"])

    # -------------------------------------------------------------------------
    # Public helpers
    # -------------------------------------------------------------------------
    @property
    def time_ms(self) -> int:
        return int(round(self.tick * self.tick_ms))

    def close(self):
        """Close files and write summary.json."""
        self._closed = True
        try:
            self._write_summary()
        except Exception:
            # Don’t break caller on summary write
            pass
        try:
            self.timeline_f.close()
            self.battery_f.close()
            self.assign_f.close()
            self.events_f.close()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Fault API
    # -------------------------------------------------------------------------
    def set_domain_fault(self, unit: str, domain: str, duration_ms: Optional[int] = None, permanent: bool = False) -> None:
        if permanent:
            self._domain_faults[(unit, domain)] = None
        else:
            self._domain_faults[(unit, domain)] = self.time_ms + int(duration_ms or 0)

    def clear_all_domain_faults(self) -> None:
        self._domain_faults.clear()

    def _domain_fault_active(self, u: str, d: str, now_ms: int) -> bool:
        key = (u, d)
        if key not in self._domain_faults:
            return False
        recover_at = self._domain_faults[key]
        if recover_at is None:
            return True
        if now_ms >= recover_at:
            self._domain_faults.pop(key, None)
            return False
        return True

    # -------------------------------------------------------------------------
    # Battery
    # -------------------------------------------------------------------------
    def _ensure_battery_initialized(self, units: List[str]) -> None:
        for u in units:
            if u not in self.battery_pct:
                self.battery_pct[u] = 100.0

    def _drain_per_role_pct(self) -> float:
        return 100.0 * (self.tick_ms / float(self.battery_life_ms))

    def _recharge_pct(self) -> float:
        # Recharge is 50% slower than minimum drain (agreed)
        return 0.5 * self._drain_per_role_pct()

    def _is_alive(self, u: str, alive: Dict[str, bool]) -> bool:
        return bool(alive.get(u, False)) and (u not in self.battery_dead)

    def _can_assign(self, u: str, alive: Dict[str, bool]) -> bool:
        return self._is_alive(u, alive) and (self.battery_pct.get(u, 0.0) > 0.0)

    def _wake_threshold_pct(self) -> float:
        if self._wake_threshold_pct_override is not None:
            return float(self._wake_threshold_pct_override)
        reserve = self.battery_reserve_pct * 100.0
        hyst = self.hysteresis_pct * 100.0
        return min(100.0, reserve + hyst)

    # -------------------------------------------------------------------------
    # EDF/LLF ordering helpers
    # -------------------------------------------------------------------------
    def _deadline(self, d: str) -> int:
        return self.last_service_tick[d] + self.max_gap_ticks

    def _slack(self, d: str) -> int:
        return self._deadline(d) - self.tick

    # -------------------------------------------------------------------------
    # Rotation / scoring
    # -------------------------------------------------------------------------
    def _is_rotation_tick(self) -> bool:
        return self.rotation_period_ms > 0 and (self.time_ms - self._last_rotation_ms) >= self.rotation_period_ms

    def _rotation_ticks(self) -> int:
        return max(1, int(self.rotation_period_ms / max(self.tick_ms, 0.0001)))

    def _cooldown_age_norm(self, u: str) -> float:
        last = self._last_assigned_tick.get(u, -10**9)
        age = max(0, self.tick - last)
        return min(1.0, age / float(self._rotation_ticks()))

    def _recent_active_flag(self, u: str) -> float:
        return 1.0 if self._last_assigned_tick.get(u, -10**9) == (self.tick - 1) else 0.0

    def _dwell_ok(self, u: str) -> bool:
        since = self._active_since_tick.get(u)
        return True if since is None else (self.tick - since) >= self.min_dwell_ticks

    def _score_unit(self, u: str, prefer_keep: bool, do_rotate: bool) -> float:
        b = max(0.0, min(100.0, float(self.battery_pct.get(u, 0.0))))
        battery_norm = b / 100.0
        cooldown = self._cooldown_age_norm(u)
        recent_penalty = self._recent_active_flag(u) if do_rotate else 0.0
        score = battery_norm + (self.cooldown_weight * cooldown) - (self.rotation_weight * recent_penalty)
        if prefer_keep and not do_rotate:
            score += self.keep_bonus
        return score

    # -------------------------------------------------------------------------
    # Candidate selection (wake hysteresis; overridable)
    # -------------------------------------------------------------------------
    def _candidates_for_domain(self, d: str, alive: Dict[str, bool], units_all: List[str], allow_override: bool) -> List[str]:
        now_ms = self.time_ms
        wake_thr = self._wake_threshold_pct()

        def ok(u: str) -> bool:
            if not self._can_assign(u, alive):
                return False
            if self._domain_fault_active(u, d, now_ms):
                return False
            if not allow_override:
                if u in self._resting_since_tick and self.battery_pct.get(u, 0.0) < wake_thr:
                    return False
            return True

        if self.universal_roles:
            return [u for u in units_all if ok(u)]

        # Pool-based fallback
        spares = self.pools.get("spares", [])
        primary = self.pools.get(d, [])
        prim_ok = [u for u in primary if ok(u)]
        spare_ok = [u for u in spares if ok(u)]
        seen = set()
        out: List[str] = []
        for u in (prim_ok + spare_ok):
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out

    # -------------------------------------------------------------------------
    # Distinctness helpers
    # -------------------------------------------------------------------------
    def _total_required_roles(self) -> int:
        return int(sum(int(self.required_map.get(d, 1)) for d in self.domains_active))

    # -------------------------------------------------------------------------
    # Logging helpers
    # -------------------------------------------------------------------------
    def _emit_event(self, kind: str, detail: str):
        """Record an event for UI/reporting.

        Guarded against shutdown races: GUI may call close() while a tick is in-flight.
        In that case files are closed and we simply stop emitting.
        """
        if getattr(self, "_closed", False):
            return
        ev = ScheduleEvent(self.tick, self.time_ms, kind, detail)
        self.events.append(ev)
        try:
            self.events_w.writerow([ev.tick, ev.time_ms, ev.kind, ev.detail])
        except ValueError:
            return
        except Exception:
            return
    def _maybe_sample(self, alive: Dict[str, bool], assign_map: Dict[str, List[str]]):
        """Write battery and assignment samples every sample_every_ticks."""
        if (self.tick % self.sample_every_ticks) != 0:
            return

        # Battery sample rows (one per unit)
        units_all = list(alive.keys())
        active_set = set()
        for d in self.domains:
            active_set.update(assign_map.get(d, []))

        for u in units_all:
            if u in self.battery_dead:
                state = "dead"
            elif not alive.get(u, False):
                state = "down"
            elif u in active_set:
                state = "active"
            else:
                state = "rest"
            self.battery_w.writerow([self.tick, self.time_ms, u, f"{self.battery_pct.get(u, 0.0):.3f}", state])

        # Distinctness metrics
        assignable = sum(1 for u in units_all if self._can_assign(u, alive))
        desired_distinct = min(self._total_roles_required, assignable)
        actual_distinct = len(set(active_set))

        row = [self.tick, self.time_ms, desired_distinct, actual_distinct]
        for d in self.domains:
            row.append(";".join(assign_map.get(d, [])))
        self.assign_w.writerow(row)

    def _write_summary(self):
        """Write summary.json with run metrics."""
        summary = {
            "ticks_total": int(self._ticks_total),
            "time_ms_total": int(self.time_ms),
            "sample_every_ticks": int(self.sample_every_ticks),
            "total_required_roles": int(self._total_roles_required),
            "distinct_ok_ticks": int(self._ticks_distinct_ok),
            "distinct_ok_pct": (float(self._ticks_distinct_ok) / float(self._ticks_total) * 100.0) if self._ticks_total else 0.0,
            "multi_role_ticks": int(self._ticks_multi_role),
            "multi_role_pct": (float(self._ticks_multi_role) / float(self._ticks_total) * 100.0) if self._ticks_total else 0.0,
            "total_assignments": int(self._total_assignments),
            "battery_dead_units": sorted(list(self.battery_dead)),
            "battery_dead_first_tick": self._battery_dead_first_tick,
            "domain_weights": self.domain_weights,
            "tick_ms": float(self.tick_ms),
            "rotation_period_ms": int(self.rotation_period_ms),
        }
        with open(self.summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)

    # -------------------------------------------------------------------------
    # Main scheduler tick
    # -------------------------------------------------------------------------
    def schedule_tick(self, alive: Dict[str, bool]) -> List[Tuple[str, str]]:
        self.tick += 1
        self._ticks_total += 1
        self.events = []

        units_all = list(alive.keys())
        self._ensure_battery_initialized(units_all)
        prev_battery = dict(self.battery_pct)

        do_rotate = self._is_rotation_tick()
        if do_rotate:
            self._last_rotation_ms = self.time_ms
            self._emit_event("rotation", "atomic rotation boundary")

        # EDF/LLF ordering
        ordered_domains = sorted(self.domains_active, key=lambda d: (self._deadline(d), self._slack(d)))

        # Capacity per unit (alive & battery>0 & not dead)
        capacity: Dict[str, int] = {u: self.capacity_per_unit for u in units_all if self._can_assign(u, alive)}

        # Distinctness target
        total_roles = self._total_roles_required
        desired_distinct = min(total_roles, len(capacity))

        used_units: Set[str] = set()
        assignments: List[Tuple[str, str]] = []
        assign_map: Dict[str, List[str]] = {d: [] for d in self.domains}

        prev_active_set: Set[str] = set()
        for d in self.domains:
            prev_active_set.update(self.prev_assign.get(d, []))

        def force_keep(u: str) -> bool:
            if u not in prev_active_set:
                return False
            if self._dwell_ok(u):
                return False
            # break dwell if critical low
            return self.battery_pct.get(u, 0.0) > self.swap_threshold_pct

        # Domain assignment loop
        for d in ordered_domains:
            need = int(self.required_map.get(d, 1))
            if need <= 0:
                continue

            prev_for_domain = set(self.prev_assign.get(d, []))

            strict = self._candidates_for_domain(d, alive, units_all, allow_override=False)
            override = self._candidates_for_domain(d, alive, units_all, allow_override=True)

            if len(strict) < need:
                strict = override
                self._emit_event("wake_override", f"{d}: wake hysteresis overridden to satisfy need={need}")

            keep_candidates: Set[str] = set()
            if not do_rotate:
                for u in prev_for_domain:
                    if u not in override:
                        continue
                    b = self.battery_pct.get(u, 0.0)
                    if force_keep(u) or (b > self.swap_threshold_pct):
                        keep_candidates.add(u)

            def sort_by_score(cands: List[str]) -> List[str]:
                return sorted(cands, key=lambda u: (-self._score_unit(u, (u in keep_candidates), do_rotate), u))

            # Partition by used/unused
            unused_strict = sort_by_score([u for u in strict if u not in used_units])
            used_strict = sort_by_score([u for u in strict if u in used_units])
            unused_override = sort_by_score([u for u in override if u not in used_units and u not in unused_strict])
            used_override = sort_by_score([u for u in override if u in used_units and u not in used_strict])

            chosen: List[str] = []

            def can_take(u: str) -> bool:
                return capacity.get(u, 0) > 0

            # A: keep among unused strict
            if not do_rotate and keep_candidates:
                for u in unused_strict:
                    if need <= 0:
                        break
                    if u not in keep_candidates or not can_take(u):
                        continue
                    capacity[u] -= 1
                    chosen.append(u)
                    used_units.add(u)
                    need -= 1

            # B: unused strict
            for u in unused_strict:
                if need <= 0:
                    break
                if u in chosen or not can_take(u):
                    continue
                capacity[u] -= 1
                chosen.append(u)
                used_units.add(u)
                need -= 1

            # C: if we still need and distinctness not reached, wake additional unused override units
            if need > 0 and len(used_units) < desired_distinct and unused_override:
                self._emit_event("distinctness_wake", f"{d}: waking additional unused units (target={desired_distinct})")
                for u in unused_override:
                    if need <= 0:
                        break
                    if not can_take(u):
                        continue
                    capacity[u] -= 1
                    chosen.append(u)
                    used_units.add(u)
                    need -= 1

            # D: used strict (multi-role)
            for u in used_strict:
                if need <= 0:
                    break
                if not can_take(u):
                    continue
                capacity[u] -= 1
                chosen.append(u)
                need -= 1

            # E: used override last resort
            if need > 0 and used_override:
                self._emit_event("wake_override_used", f"{d}: using used override candidates (multi-role)")
                for u in used_override:
                    if need <= 0:
                        break
                    if not can_take(u):
                        continue
                    capacity[u] -= 1
                    chosen.append(u)
                    need -= 1
            if need > 0:
                # Unable to satisfy this domain on this tick
                self._emit_event("unmet_requirements", f"{d}: need_remaining={need}")
                # Allow sim to continue; GAP_EXCEEDED will trigger mission failure when strict

            # Commit
            for u in chosen:
                assignments.append((d, u))
                assign_map[d].append(u)
                self.last_service_tick[d] = self.tick
                self._last_assigned_tick[u] = self.tick

        # --- Requirement coverage / contingency tracking ---
        unmet = []
        for d in self.domains_active:
            need_d = int(self.required_map.get(d, 1))
            got_d = len(assign_map.get(d, []))
            if need_d > 0 and got_d < need_d:
                unmet.append(f"{d}: need={need_d}, got={got_d}")

        if unmet:
            self._unmet_requirements_streak += 1
            self._emit_event("unmet_requirements", "; ".join(unmet))
            if self._unmet_requirements_streak > self.max_gap_ticks:
                msg = "CRITICAL mission failure: unmet requirements for > max_gap_ticks: " + "; ".join(unmet)
                self._emit_event("mission_failure", msg)
                if self.strict_mission_failure:
                    raise RuntimeError(msg)
        else:
            self._unmet_requirements_streak = 0

                
        # Hard gap enforcement
        for d in self.domains_active:
            gap = self.tick - self.last_service_tick[d]
            if gap > self.max_gap_ticks:
                msg = (
                    f"CRITICAL mission failure @tick={self.tick}: "
                    f"GAP_EXCEEDED domain={d} gap={gap} max={self.max_gap_ticks}"
                )
                self._emit_event("mission_failure", msg)
                if self.strict_mission_failure:
                    raise RuntimeError(msg)
                # if strict is False, continue running but still record the event


        # Active/rest sets
        active_set = {u for _, u in assignments}
        self.rest_units = {u for u in units_all if self._is_alive(u, alive) and u not in active_set}

        if self.rest_domain is not None:
            rest_list = [u for u in units_all if self._is_alive(u, alive) and u not in active_set and u not in self.battery_dead]
            assign_map[self.rest_domain] = sorted(rest_list)

        # Dwell tracking
        for u in active_set:
            if u not in prev_active_set:
                self._active_since_tick[u] = self.tick
        for u in prev_active_set:
            if u not in active_set:
                self._active_since_tick.pop(u, None)

        # Rest bookkeeping
        for u in self.rest_units:
            if u not in self._resting_since_tick:
                self._resting_since_tick[u] = self.tick
        for u in active_set:
            self._resting_since_tick.pop(u, None)

        # Multi-role metric
        counts: Dict[str, int] = {}
        for _, u in assignments:
            counts[u] = counts.get(u, 0) + 1
        if any(v > 1 for v in counts.values()):
            self._ticks_multi_role += 1

        # Distinctness metric (tick-level)
        assignable = sum(1 for u in units_all if self._can_assign(u, alive))
        desired_distinct_tick = min(total_roles, assignable)
        actual_distinct_tick = len(active_set)
        if desired_distinct_tick == 0 or actual_distinct_tick >= desired_distinct_tick:
            self._ticks_distinct_ok += 1

        # Battery update (weighted drain) + dead handling
        base_drain = self._drain_per_role_pct()
        recharge = self._recharge_pct()

        drain_per_unit: Dict[str, float] = {u: 0.0 for u in units_all}
        for d, u in assignments:
            w = float(self.domain_weights.get(d, 1.0))
            drain_per_unit[u] += base_drain * w

        for u in units_all:
            if not self._is_alive(u, alive):
                continue  # frozen while down or dead

            if drain_per_unit.get(u, 0.0) > 0.0:
                new_b = self.battery_pct.get(u, 0.0) - drain_per_unit[u]
                if new_b <= 0.0:
                    self.battery_pct[u] = 0.0
                    if u not in self.battery_dead:
                        self.battery_dead.add(u)
                        self._battery_dead_first_tick[u] = self.tick
                        self._emit_event("battery_dead", f"{u} reached 0% and is permanently dead")
                else:
                    self.battery_pct[u] = new_b
            else:
                # Recharge only if alive & not dead
                rest_w = max(0.0, float(self.domain_weights.get(self.rest_domain, 1.0))) if self.rest_domain is not None else 1.0
                self.battery_pct[u] = min(100.0, self.battery_pct.get(u, 0.0) + recharge * rest_w)
        # Low battery warnings (optionally throttled)
        # Default behavior (every_ms=0) preserves prior behavior (emit every tick while <= threshold).
        for u in active_set:
            if u in self.battery_dead:
                continue
            b = self.battery_pct.get(u, 0.0)
            if b <= self.swap_threshold_pct:
                if self.low_battery_event_crossing_only:
                    prev_b = prev_battery.get(u, b)
                    if prev_b <= self.swap_threshold_pct:
                        continue
                if self.low_battery_event_every_ticks and self.low_battery_event_every_ticks > 1:
                    last_t = self._last_low_battery_warn_tick.get(u, -10**9)
                    if (self.tick - last_t) < self.low_battery_event_every_ticks:
                        continue
                    self._last_low_battery_warn_tick[u] = self.tick
                self._emit_event("low_battery_active", f"{u} active <= {self.swap_threshold_pct:.1f}% ({b:.1f}%)")

        # Timeline logging (only on assignment changes)
        changed = False
        for d in self.domains:
            prev = self.prev_assign.get(d, [])
            curr = assign_map.get(d, [])
            if prev != curr:
                self.timeline_w.writerow([self.tick, self.time_ms, d, ";".join(curr), "assignments"])
                changed = True

        self.prev_assign = {d: assign_map.get(d, [])[:] for d in self.domains}
        self.last_assign_map = {d: assign_map.get(d, [])[:] for d in self.domains}

        # Update summary counters
        self._total_assignments += len(assignments)

        # Sample logs every N ticks
        self._maybe_sample(alive, assign_map)

        return assignments
