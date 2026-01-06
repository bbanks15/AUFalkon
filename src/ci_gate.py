
import sys, json, argparse, glob, os, subprocess, ast

def run_cmd(cmd):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate()
    return p.returncode, out, err

def parse_runner_output(stdout_text: str, stderr_text: str, rc: int):
    """
    Prefer strict JSON. Fallback to python literal dict.
    If parsing fails, return a structured FAIL object with stdout/stderr embedded.
    """
    stdout_text = (stdout_text or "").strip()
    stderr_text = (stderr_text or "").strip()

    # Strict JSON first (expected once mission_runner prints json.dumps)
    try:
        if stdout_text:
            return json.loads(stdout_text)
    except Exception:
        pass

    # Fallback: python literal dict (legacy runner output)
    try:
        if stdout_text:
            obj = ast.literal_eval(stdout_text)
            if isinstance(obj, dict):
                return obj
    except Exception:
        pass

    # If we got here, output was not parseable. Preserve evidence.
    return {
        "status": "FAIL" if rc != 0 else "UNKNOWN",
        "error": (
            f"Unparseable runner output. rc={rc}\n"
            f"STDOUT:\n{stdout_text}\n"
            f"STDERR:\n{stderr_text}"
        )
    }

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
            all_failures.append({'mission': m, 'stage': 'validator_failed', 'error': err.strip() or out.strip()})
            continue

        try:
            v = json.loads(out)
        except Exception:
            all_failures.append({'mission': m, 'stage': 'validator_bad_json', 'error': out.strip()})
            continue

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

            rj = parse_runner_output(out2, err2, rc2)

            sweep_results.append({
                'faults': faults,
                'rc': rc2,
                'status': rj.get('status', 'UNKNOWN'),
                'error': rj.get('error', ''),
                'logs_dir': logs_dir,
                # Keep evidence if something went wrong
                'stderr': (err2 or '').strip() if (rc2 != 0 or rj.get('status') != 'PASS') else ''
            })

            if rj.get('status') != 'PASS':
                all_failures.append({
                    'mission': m,
                    'stage': f'fault_sweep_failure_faults={faults}',
                    'error': rj.get('error', '') or (err2 or '').strip()
                })
                break

        summary[m] = {'validator': v, 'sweep': sweep_results}

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
