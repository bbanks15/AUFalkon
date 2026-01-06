# RTOS PORT PLAN (Minimal)

This PoC models the control-layer assignment logic. A minimal MCU/RTOS port would use:

## Tasks (priority high → low)
1. DomainServiceTask (highest): runs each tick, executes scheduler step, emits assignment table.
2. DeviceHealthTask: updates alive/down status; integrates fault detection.
3. LogWriterTask: flushes change-only logs (or telemetry) outside the critical path.
4. ConfigManagerTask: applies mission changes between ticks (double-buffered config).

## Jitter control
- Hardware timer ISR drives tick.
- Scheduler uses bounded loops over domains/units (O(D+N)).
- No dynamic allocation in tick path.
- Optional: measure WCET with cycle counter.
