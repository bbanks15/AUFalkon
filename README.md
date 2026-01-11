# AUFalkon – Control-Layer Deadline Simulator (PoC)

This repository contains a deadline-first control-layer simulation with:
- Deadline scheduler (EDF/LLF + failover pools)
- GUI simulator (temporary + permanent failure buttons)
- Mission runner (headless; change-only CSV logs)
- CI gate with fault sweep 0..Fmax and artifact upload
- Pre-commit / hook mission validation

## Folder layout
- `src/` Python sources
- `missions/` mission JSONs (CI scans `missions/**/mission*.json`)
- `docs/` assumptions and RTOS notes
- `.github/workflows/` GitHub Actions workflows

> Note: GitHub runners are case-sensitive: folder name must be `missions` (lowercase).

## Quickstart (local)
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# optional: enable pre-commit
pre-commit install
pre-commit run --all-files

# run headless
python src/mission_runner.py missions/fleet12/mission_fleet12_deadline_ms1.json --ticks 200 --logs_dir runner_logs_demo --initial_faults 0

# run GUI
python -m src.gui_sim
```

## Quickstart (CI)
- Push to GitHub.
- Go to Actions tab.
- Run `control-layer-ci-gate` (auto-runs on push/PR).
- Download artifacts:
  - `fault_sweep_summary.json`
  - `runner_logs_*` directories (CSV logs inside)

## Mission validation (hook)
Run locally:
```bash
python hooks/validate_missions.py --glob "DemoProfile/*_mission.json,missions/**/mission*.json"
```
