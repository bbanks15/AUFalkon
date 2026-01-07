
import csv
import os
from typing import Dict, List, Tuple, Any
from collections import deque


class _MinCostMaxFlow:
    class Edge:
        __slots__ = ("to", "rev", "cap", "cost")
        def __init__(self, to: int, rev: int, cap: int, cost: int):
            self.to = to
            self.rev = rev
            self.cap = cap
            self.cost = cost

    def __init__(self, n: int):
        self.n = n
        self.g: List[List[_MinCostMaxFlow.Edge]] = [[] for _ in range(n)]

    def add_edge(self, fr: int, to: int, cap: int, cost: int):
        fwd = _MinCostMaxFlow.Edge(to, len(self.g[to]), cap, cost)
        rev = _MinCostMaxFlow.Edge(fr, len(self.g[fr]), 0, -cost)
        self.g[fr].append(fwd)
        self.g[to].append(rev)

    def min_cost_flow(self, s: int, t: int, maxf: int) -> Tuple[int, int]:
        n = self.n
        INF = 10**15
        flow = 0
        cost = 0
        pot = [0] * n

        while flow < maxf:
            dist = [INF] * n
            prevv = [-1] * n
            preve = [-1] * n
            dist[s] = 0

            inq = [False] * n
            q = deque([s])
            inq[s] = True

            while q:
                v = q.popleft()
                inq[v] = False
                for i, e in enumerate(self.g[v]):
                    if e.cap <= 0:
                        continue
                    nd = dist[v] + e.cost + pot[v] - pot[e.to]
                    if nd < dist[e.to]:
                        dist[e.to] = nd
                        prevv[e.to] = v
                        preve[e.to] = i
                        if not inq[e.to]:
                            q.append(e.to)
                            inq[e.to] = True

            if dist[t] == INF:
                break

            for v in range(n):
                if dist[v] < INF:
                    pot[v] += dist[v]

            addf = maxf - flow
            v = t
            while v != s:
                pv = prevv[v]
                pe = preve[v]
                if pv < 0:
                    addf = 0
                    break
                addf = min(addf, self.g[pv][pe].cap)
                v = pv
            if addf <= 0:
                break

            v = t
            while v != s:
                pv = prevv[v]
                pe = preve[v]
                e = self.g[pv][pe]
                e.cap -= addf
                self.g[v][e.rev].cap += addf
                v = pv

            flow += addf
            cost += addf * pot[t]

        return flow, cost


class DeadlineScheduler:
    """
    Deterministic, deadline-first assignment engine.

    Evidence logging:
    - timeline.csv: per-domain changes (mode, atomic)
    - matrix.csv: per-tick assignment row
    - handoff_log.csv: removed/added per domain + REST + multi-role + mode

    Fix included:
    - Prevent assigning the SAME device twice to the SAME domain in a single tick,
      even when using slot2 in contingency.
    """

    def __init__(
        self,
        domains: List[str],
        pools: Dict[str, List[str]],
        required_map: Dict[str, int],
        max_gap_ticks: int,
        tick_ms: float,
        capacity_per_unit: int = 2,
        logs_dir: str = "logs_deadline"
    ):
        self.domains = list(domains)
        self.pools = pools
        self.required_map = dict(required_map)
        self.max_gap_ticks = int(max_gap_ticks)
        self.tick_ms = float(tick_ms)
        self.capacity_per_unit = int(capacity_per_unit)

        self.tick = 0
        self.last_service_tick: Dict[str, int] = {d: 0 for d in self.domains}
        self.prev_assign: Dict[str, List[str]] = {d: [] for d in self.domains}

        self.total_ticks = 0
        self.contingency_ticks = 0
        self.last_mode = "NORMAL"

        self._last_tick_details: Dict[str, Any] = {}

        os.makedirs(logs_dir, exist_ok=True)
        self.logs_dir = logs_dir

        self.timeline_f = open(f"{logs_dir}/timeline.csv", "w", newline="", encoding="utf-8")
        self.matrix_f = open(f"{logs_dir}/matrix.csv", "w", newline="", encoding="utf-8")
        self.handoff_f = open(f"{logs_dir}/handoff_log.csv", "w", newline="", encoding="utf-8")

        self.timeline_w = csv.writer(self.timeline_f)
        self.matrix_w = csv.writer(self.matrix_f)
        self.handoff_w = csv.writer(self.handoff_f)

        self.timeline_w.writerow(["tick", "mode", "domain", "assigned", "atomic", "reason"])
        self.matrix_w.writerow(["tick", "mode"] + [f"{d}_devices" for d in self.domains])
        self.handoff_w.writerow([
            "tick", "mode", "domain",
            "removed", "added", "atomic",
            "alive_units", "rest_units", "multirole_units"
        ])

    def close(self):
        try:
            self.timeline_f.close()
            self.matrix_f.close()
            self.handoff_f.close()
        except Exception:
            pass

    def get_run_summary(self) -> Dict[str, Any]:
        total = max(1, self.total_ticks)
        return {
            "total_ticks": self.total_ticks,
            "contingency_ticks": self.contingency_ticks,
            "contingency_rate": self.contingency_ticks / total,
            "last_mode": self.last_mode,
        }

    def get_last_tick_details(self) -> Dict[str, Any]:
        return dict(self._last_tick_details) if self._last_tick_details else {}

    def _deadline(self, d: str) -> int:
        return self.last_service_tick[d] + self.max_gap_ticks

    def _slack(self, d: str) -> int:
        return self._deadline(d) - self.tick

    def _ordered_domains(self) -> List[str]:
        return sorted(self.domains, key=lambda d: (self._deadline(d), self._slack(d), d))

    def _candidates_for(self, domain: str, alive: Dict[str, bool]) -> List[str]:
        primary = [u for u in self.pools.get(domain, []) if alive.get(u, False)]
        backup = [u for u in self.pools.get("spares", []) if alive.get(u, False)]
        out, seen = [], set()
        for u in primary + backup:
            if u not in seen:
                out.append(u)
                seen.add(u)
        return out

    def _build_and_solve(self, alive: Dict[str, bool], allow_second_slot: bool) -> Tuple[Dict[str, List[str]], bool]:
        ordered_domains = self._ordered_domains()
        alive_units = sorted([u for u, ok in alive.items() if ok])

        need_total = sum(self.required_map[d] for d in self.domains)

        # global slot capacity
        slots_per_unit = 2 if (allow_second_slot and self.capacity_per_unit >= 2) else 1
        if len(alive_units) * slots_per_unit < need_total:
            return {}, False

        # per-domain feasibility: must have enough DISTINCT eligible devices for that domain
        for d in self.domains:
            cand = self._candidates_for(d, alive)
            if len(cand) < self.required_map[d]:
                return {}, False

        # Build slots: (unit, slot#)
        slots: List[Tuple[str, int]] = []
        for u in alive_units:
            slots.append((u, 1))
            if allow_second_slot and self.capacity_per_unit >= 2:
                slots.append((u, 2))

        # --- Graph nodes ---
        # S -> domain nodes (cap=need)
        # domain -> gate(domain,unit) (cap=1)   <-- NEW: prevents same unit twice per domain
        # gate(domain,unit) -> slot(unit,1/2) (cap=1, slot2 penalized)
        # slot -> T (cap=1)
        S = 0
        dom_offset = 1
        dom_count = len(ordered_domains)

        # Build gates list deterministically: for each domain, for each candidate unit (in candidate order)
        gates: List[Tuple[str, str]] = []  # (domain, unit)
        gate_index: Dict[Tuple[str, str], int] = {}

        for d in ordered_domains:
            cand = self._candidates_for(d, alive)
            for u in cand:
                key = (d, u)
                if key not in gate_index:
                    gate_index[key] = len(gates)
                    gates.append(key)

        gate_offset = dom_offset + dom_count
        slot_offset = gate_offset + len(gates)
        T = slot_offset + len(slots)
        N = T + 1

        mcmf = _MinCostMaxFlow(N)

        # Source -> domain
        for i, d in enumerate(ordered_domains):
            mcmf.add_edge(S, dom_offset + i, self.required_map[d], 0)

        # Slot -> sink
        for j in range(len(slots)):
            mcmf.add_edge(slot_offset + j, T, 1, 0)

        prev_set = {(d, u) for d, us in self.prev_assign.items() for u in us}

        CHURN_COST = 1
        STABLE_COST = 0
        SLOT2_PENALTY = 100  # big => minimize multi-role

        def tiny_tiebreak(u: str, slot_idx: int) -> int:
            base = ord(u[0]) if u else 0
            return (base % 7) + (slot_idx - 1)

        # Domain -> Gate (cap=1 ensures domain cannot take same unit twice)
        for i, d in enumerate(ordered_domains):
            dn = dom_offset + i
            cand = self._candidates_for(d, alive)
            for u in cand:
                gn = gate_offset + gate_index[(d, u)]
                cost = STABLE_COST if (d, u) in prev_set else CHURN_COST
                # cost sits here so it applies regardless of which slot is chosen
                mcmf.add_edge(dn, gn, 1, cost)

        # Gate -> Slots (slot2 penalized)
        # Each gate represents choosing unit u for domain d exactly once; it can route to slot1 or slot2.
        for (d, u), gi in gate_index.items():
            gn = gate_offset + gi
            for j, (uu, slot_idx) in enumerate(slots):
                if uu != u:
                    continue
                cost = 0
                if slot_idx == 2:
                    cost += SLOT2_PENALTY
                cost += tiny_tiebreak(u, slot_idx)
                mcmf.add_edge(gn, slot_offset + j, 1, cost)

        flow, _ = mcmf.min_cost_flow(S, T, need_total)
        if flow != need_total:
            return {}, False

        # Extract assignment: a gate used implies that domain selected that unit (exactly once),
        # then we infer unit from the gate key.
        assign_map: Dict[str, List[str]] = {d: [] for d in self.domains}

        # For each domain node, find outgoing edges to gates where reverse cap > 0 => used
        for i, d in enumerate(ordered_domains):
            dn = dom_offset + i
            for e in mcmf.g[dn]:
                if gate_offset <= e.to < slot_offset:
                    rev = mcmf.g[e.to][e.rev]
                    if rev.cap > 0:
                        gate_i = e.to - gate_offset
                        dd, uu = gates[gate_i]
                        # dd should equal d
                        if dd == d:
                            assign_map[d].append(uu)

        # Final sanity: each domain got exactly need distinct units
        for d in self.domains:
            need = self.required_map[d]
            if len(assign_map[d]) != need:
                return {}, False
            if len(set(assign_map[d])) != len(assign_map[d]):
                # Should never happen now
                return {}, False

        # Determine multi-role usage (unit used across >=2 domains)
        unit_roles = {}
        for d in self.domains:
            for u in assign_map[d]:
                unit_roles[u] = unit_roles.get(u, 0) + 1
        multirole_used = any(v > 1 for v in unit_roles.values())

        return assign_map, multirole_used

    def schedule_tick(self, alive: Dict[str, bool]) -> List[Tuple[str, str]]:
        self.tick += 1
        self.total_ticks += 1

        # Pass 1: single-role only
        assign_map, multirole_used = self._build_and_solve(alive, allow_second_slot=False)
        mode = "NORMAL"

        # Pass 2: allow second slot
        if not assign_map:
            assign_map, multirole_used = self._build_and_solve(alive, allow_second_slot=True)
            mode = "CONTINGENCY"

        if not assign_map:
            starving = []
            for d in self.domains:
                cand = self._candidates_for(d, alive)
                starving.append(f"{d}:need={self.required_map[d]} eligible_alive={len(cand)}")
            raise RuntimeError(
                f"INVARIANT FAILURE @tick={self.tick}: INFEASIBLE "
                f"total_need={sum(self.required_map.values())} "
                f"alive={sum(1 for v in alive.values() if v)} "
                f"details=({'; '.join(starving)})"
            )

        # Record service
        for d in self.domains:
            self.last_service_tick[d] = self.tick

        # Verify hard gap
        for d in self.domains:
            gap = self.tick - self.last_service_tick[d]
            if gap > self.max_gap_ticks:
                raise RuntimeError(
                    f"INVARIANT FAILURE @tick={self.tick}: GAP_EXCEEDED domain={d} gap={gap} max={self.max_gap_ticks}"
                )

        # REST + multirole lists for evidence
        alive_units = sorted([u for u, ok in alive.items() if ok])
        assigned_units = sorted({u for d in self.domains for u in assign_map[d]})
        rest_units = sorted(set(alive_units) - set(assigned_units))

        unit_roles = {}
        for d in self.domains:
            for u in assign_map[d]:
                unit_roles[u] = unit_roles.get(u, 0) + 1
        multirole_units = sorted([u for u, c in unit_roles.items() if c > 1])

        if mode == "CONTINGENCY" and multirole_used:
            self.contingency_ticks += 1
            self.last_mode = "CONTINGENCY"
        else:
            self.last_mode = "NORMAL"

        # Handoff transitions (atomic by construction)
        handoffs = []
        for d in self.domains:
            prev = set(self.prev_assign.get(d, []))
            curr = set(assign_map.get(d, []))
            removed = sorted(prev - curr)
            added = sorted(curr - prev)
            atomic = True
            if removed or added:
                handoffs.append({"domain": d, "removed": removed, "added": added, "atomic": atomic})
                self.handoff_w.writerow([
                    self.tick, mode, d,
                    ";".join(removed), ";".join(added), "YES",
                    ";".join(alive_units),
                    ";".join(rest_units),
                    ";".join(multirole_units)
                ])

        self._last_tick_details = {
            "tick": self.tick,
            "mode": mode,
            "alive_units": alive_units,
            "rest_units": rest_units,
            "multirole_units": multirole_units,
            "handoffs": handoffs
        }

        # timeline + matrix (change-only)
        changed = False
        for d in self.domains:
            prev = self.prev_assign.get(d, [])
            curr = assign_map.get(d, [])
            if prev != curr:
                self.timeline_w.writerow([self.tick, mode, d, ";".join(curr), "YES", "assignments"])
                changed = True

        if changed:
            row = [self.tick, mode] + [";".join(assign_map[d]) for d in self.domains]
            self.matrix_w.writerow(row)

        self.prev_assign = {d: assign_map.get(d, [])[:] for d in self.domains}

        # Return list of (domain, unit)
        assignments: List[Tuple[str, str]] = []
        for d in self.domains:
            for u in assign_map[d]:
                assignments.append((d, u))
        return assignments
