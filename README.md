
# Control-Layer Deadline Simulator (PoC)

This repo is a **deadline-first** control-layer simulation with:
- **Deadline scheduler** (EDF/LLF + failover pools)
- **GUI** to simulate **temporary** and **permanent** unit failures
- **Mission runner** (headless) that produces **change-only** CSV logs
- **Dynamic CI gate** with **fault sweep (0..Fmax)**
- **Pre-commit hook** to enforce mission consistency

## Folder layout
- `src/` — Python source
- `missions/` — mission JSON files (name them `mission_*.json` for CI)
- `docs/` — assumptions + RTOS port notes
- `.github/workflows/` — GitHub Actions CI gate
- `hooks/` — pre-commit hooks

## Quickstart (local)
### 1) Create a virtual environment (recommended)
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate
```

### 2) Install dependencies
```bash
pip install -r requirements.txt
```

### 3) (Optional) Enable pre-commit
```bash
pre-commit install
pre-commit run --all-files
```

### 4) Run the headless mission runner
```bash
python src/mission_runner.py missions/mission_example_deadline_ms1.json --ticks 200 --logs_dir runner_logs_example --initial_faults 0
```
Outputs:
- `runner_logs_example/timeline.csv`
- `runner_logs_example/matrix.csv`

### 5) Run the GUI
```bash
python src/gui_sim.py
```
Then:
- Load `missions/mission_example_deadline_ms1.json`
- Click **Start**
- Use **Temporary Fail** (duration in ms) or **Permanent Fail**, then **Step**

GUI logs:
- `gui_logs/timeline.csv`
- `gui_logs/matrix.csv`

## CI Gate (GitHub Actions)
CI runs on push/PR and:
1) runs pre-commit checks
2) runs **fault sweep** from **0..Fmax** for each `missions/mission_*.json`
3) uploads CSV logs and `fault_sweep_summary.json` as artifacts

## Fault sweep
The sweep simulates **N simultaneous permanent unit failures** by starting the mission with the first N units forced down.
This produces a `fault_sweep_summary.json` with pass/fail status for each N.

---

## Notes
- Assumes `tick_ms = 1.0` and `constraints.max_gap_ms = 10` (10ms hard deadline per domain).
- Scheduler enforces deadlines first (no fairness tuning).
