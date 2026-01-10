# Missions (Official Suite)

This repository uses **Option B** for missions:

```
missions/
  fleet4/
  fleet5/
  fleet12/
```

## Naming convention

`mission_fleet{N}_{scenario}_deadline_ms{tick_ms}.json`

- `N`: 4, 5, or 12
- `tick_ms`: typically `1`; `jitter_stress` uses `5`

## Scenarios (9 per fleet)

- baseline
- battery_stress
- rotation_stress
- gap_recovery
- gap_failure
- multi_role_stress
- pool_constraint_stress
- recharge_priority_stress
- jitter_stress

## Key semantics

### Mission rotation is authoritative

The GUI maps `mission.rotation` into scheduler knobs:

- `rotation.rest_duration_ms` → `rotation_period_ms`
- `rotation.min_dwell_ms` → `min_dwell_ticks = round(min_dwell_ms / tick_ms)`

### Gap window is mission-authoritative

The GUI derives the gap window from the mission:

- `constraints.max_gap_ms` → `max_gap_ticks = max_gap_ms / tick_ms`

By default, the UI shows WARNING/CRITICAL banners but does not stop unless you enable **Fail immediately on gap**.

### REST domain

All missions include a `rest` domain lane:

- REST is **reporting-only** (never required)
- `domain_weights.rest` scales recharge while resting

## Run examples

```powershell
python -m src.gui_sim missions\fleet4\mission_fleet4_baseline_deadline_ms1.json
python -m src.gui_sim missions\fleet12\mission_fleet12_gap_failure_deadline_ms1.json
python -m src.gui_sim missions\fleet5\mission_fleet5_jitter_stress_deadline_ms5.json
```
