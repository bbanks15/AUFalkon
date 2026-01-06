# AUFalkon – Control-Layer Deadline Simulator (PoC)

This repository contains a deadline-first control-layer simulation with:
- Deadline scheduler (EDF/LLF + failover pools)
- GUI simulator (temporary + permanent failure buttons)
- Mission runner (headless; change-only CSV logs)
- CI gate with fault sweep 0..Fmax and artifact upload
- Pre-commit mission validation hook

## Folder layout
- `src/` Python sources
- `missions/` mission JSONs (CI scans `missions/mission_*.json`)
- `docs/` assumptions and RTOS notes
- `.github/workflows/` GitHub Actions workflows

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
python src/mission_runner.py missions/mission_fleet12_deadline_ms1.json --ticks 200 --logs_dir runner_logs_demo --initial_faults 0

# run GUI
python src/gui_sim.py
```

## Quickstart (CI)
- Push to GitHub.
- Go to Actions tab.
- Run `control-layer-ci-gate` (auto-runs on push/PR).
- Download artifacts:
  - `fault_sweep_summary.json`
  - `timeline_*_faultsN.csv`, `matrix_*_faultsN.csv`
