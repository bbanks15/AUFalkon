import sys, json, argparse, glob, os, subprocess


def run_cmd(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    return p.returncode, out, err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--missions_glob', default='missions/mission_*.json')
    ap.add_argument('--ticks', type=int, default=200)
    ap.add_argument('--sweep', action='store_true')
    ap.add_argument('--capacity_per_unit', type=int, default=2)
    args = ap.parse_args()

    missions = sorted(glob.glob(args.missions_glob))
    if not missions:
        print(f'No mission files found matching {args.missions_glob}')
        sys.exit(1)

    all_failures = []
    summary = {}

    for m in missions:
        # Validate mission and compute Fmax
        rc, out, err = run_cmd([sys.executable, 'src/mission_validator.py', m, '--capacity', str(args.capacity_per_unit)])
        if rc != 0:
            all_failures.append({'mission': m, 'stage': 'validator_failed', 'error': err})
            continue
        v = json.loads(out)
        if not v.get('feasible', False):
            all_failures.append({'mission': m, 'stage': 'infeasible', 'error': v})
            summary[m] = {'validator': v, 'sweep': []}
            continue

        fmax = int(v.get('Fmax', 0))
        sweep_to = fmax if args.sweep else 0
        sweep_results = []

        base = os.path.basename(m)
        bn = os.path.splitext(base)[0]

        for faults in range(0, sweep_to + 1):
            logs_dir = f'runner_logs_{bn}_faults{faults}'
            rc2, out2, err2 = run_cmd([
                sys.executable, 'src/mission_runner.py', m,
                '--ticks', str(args.ticks),
                '--logs_dir', logs_dir,
                '--initial_faults', str(faults),
                '--capacity_per_unit', str(args.capacity_per_unit)
            ])

            # mission_runner prints a python dict; normalize
            try:
                rj = json.loads(out2.replace("'", '"'))
            except Exception:
                rj = {'status': 'UNKNOWN', 'error': out2.strip()}

            sweep_results.append({
                'faults': faults,
                'status': rj.get('status', 'UNKNOWN'),
                'error': rj.get('error', ''),
                'logs_dir': logs_dir
            })

            if rj.get('status') != 'PASS':
                all_failures.append({'mission': m, 'stage': f'fault_sweep_failure_faults={faults}', 'error': rj.get('error','')})
                # Stop at first failure for efficiency
                break

        summary[m] = {
            'validator': v,
            'sweep': sweep_results
        }

    with open('fault_sweep_summary.json', 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2)

    if all_failures:
        print('CI GATE FAILURES:')
        for x in all_failures:
            print(' -', x)
        sys.exit(2)

    print('CI GATE: PASS')
    sys.exit(0)


if __name__ == '__main__':
    main()
