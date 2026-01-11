Check battery recharge during rest
=================================

This small helper runs a mission headless and verifies that a monitored unit's battery
increases after it transitions from active to rest.

Usage:

```bash
python scripts/check_battery_rest.py missions/fleet4/mission_fleet4_gap_recovery_deadline_ms1.json --ticks 2000
```

Options:
- `--unit`: unit id to monitor (defaults to first unit in mission)
- `--ticks`: number of ticks to run (default 2000)
- `--logs_dir`: logs output directory (default `runner_batt_check`)

Exit codes:
- `0` PASS (battery increased during rest)
- non-zero on fail or error
