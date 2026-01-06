# ASSUMPTIONS & LIMITS (PoC)

- tick_ms: **1.0 ms**
- max_gap_ms: **10 ms** (hard deadline per domain)
- required_active_per_domain: fleet-specific (see each mission file)
- capacity_per_unit: **2 domains per tick** (PoC assumption)

## Feasibility (honest impossibility)
A mission is feasible if:

`alive_units * capacity_per_unit >= domain_count * required_active_per_domain`

If this fails under a fault sweep step, the CI gate fails and the summary reports the first failing fault count.

## What the guarantee actually is
- If feasible: all domains are covered within the hard gap and assignments always satisfy required_active_per_domain.
- If not feasible: continuity becomes mathematically impossible; we fail fast with explicit reasons.
