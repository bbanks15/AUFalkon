
import json, argparse

def validate(mission_path: str, capacity_per_device: int = 2):
    with open(mission_path, 'r', encoding='utf-8') as f:
        mission = json.load(f)

    units = mission.get('units', [])
    domains = mission.get('domains', [])
    required = int(mission.get('required_active_per_domain', 1))
    pools = mission.get('domain_pools', {})
    spares = pools.get('spares', [])

    # Sanity: pools referencing unknown units
    unit_set = set(units)
    unknown = {}
    for d in domains:
        bad = [u for u in pools.get(d, []) if u not in unit_set]
        if bad:
            unknown[d] = bad
    bad_spares = [u for u in spares if u not in unit_set]
    if bad_spares:
        unknown["spares"] = bad_spares

    def feasible_at_faults(faults: int) -> (bool, str):
        # Deterministic initial faults: first N units dead
        alive = set(units[faults:])

        # Global capacity check
        total_need = len(domains) * required
        total_cap = len(alive) * capacity_per_device
        if total_cap < total_need:
            return False, f"global_capacity total_need={total_need} total_cap={total_cap}"

        # Per-domain eligible-alive check (primary + spares)
        for d in domains:
            primary = [u for u in pools.get(d, []) if u in alive]
            backup = [u for u in spares if u in alive]
            # de-dup
            seen = set()
            cand = []
            for u in primary + backup:
                if u not in seen:
                    cand.append(u); seen.add(u)
            if len(cand) < required:
                return False, f"domain={d} need={required} eligible_alive={len(cand)}"
        return True, "ok"

    # Find maximum faults that remains feasible
    fmax = 0
    reason_at_fail = ""
    for f in range(0, len(units) + 1):
        ok, reason = feasible_at_faults(f)
        if ok:
            fmax = f
        else:
            reason_at_fail = f"first_fail_faults={f} reason={reason}"
            break

    return {
        "mission": mission_path,
        "fleet_devices": len(units),
        "domains": len(domains),
        "capacity_per_device": capacity_per_device,
        "required_active_per_domain": required,
        "feasible": (fmax >= 0),
        "Fmax": fmax,
        "deterministic_fault_model": "first N units are permanently down",
        "unknown_pool_units": unknown,
        "fail_reason": reason_at_fail
    }

if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('mission')
    ap.add_argument('--capacity', type=int, default=2)
    args = ap.parse_args()

    result = validate(args.mission, capacity_per_device=args.capacity)
    print(json.dumps(result, indent=2))
