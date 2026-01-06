#!/usr/bin/env python3
"""Pre-commit hook to validate mission files.

Rules (PoC):
- missions must be valid JSON
- must include: tick_ms, constraints.max_gap_ms, required_active_per_domain, domains, units, domain_pools
- tick_ms must be 1.0
- max_gap_ms must be 10
- if fleet_devices is present, it must equal len(units)

This hook intentionally DOES NOT force required_active_per_domain to be identical across missions,
so you can have fleet-specific missions (e.g., 4/5 => 1, 12 => 2).
"""

import sys, json
from pathlib import Path

REQUIRED_KEYS = ['tick_ms','domains','units','domain_pools','required_active_per_domain','constraints']


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        return {'__error__': str(e)}


def main(argv):
    files = [Path(p) for p in argv if p.endswith('.json')]
    missions = [p for p in files if p.name.startswith('mission_')]
    if not missions:
        return 0

    errors = []
    for p in missions:
        m = load(p)
        if '__error__' in m:
            errors.append(f"{p}: invalid JSON ({m['__error__']})")
            continue
        for k in REQUIRED_KEYS:
            if k not in m:
                errors.append(f"{p}: missing key '{k}'")
        if 'constraints' in m and isinstance(m['constraints'], dict):
            if 'max_gap_ms' not in m['constraints']:
                errors.append(f"{p}: missing constraints.max_gap_ms")
            else:
                if int(m['constraints']['max_gap_ms']) != 10:
                    errors.append(f"{p}: constraints.max_gap_ms must be 10")
        if 'tick_ms' in m:
            try:
                if float(m['tick_ms']) != 1.0:
                    errors.append(f"{p}: tick_ms must be 1.0")
            except Exception:
                errors.append(f"{p}: tick_ms must be numeric")
        if 'fleet_devices' in m:
            try:
                if int(m['fleet_devices']) != len(m.get('units', [])):
                    errors.append(f"{p}: fleet_devices must equal len(units)")
            except Exception:
                errors.append(f"{p}: fleet_devices must be integer")

    if errors:
        for e in errors:
            print('HOOK ERROR:', e)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
