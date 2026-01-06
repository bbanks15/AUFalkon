import json, argparse, math


def feasible(n_devices: int, d_count: int, capacity_per_device: int, required_per_domain: int) -> bool:
    return (n_devices * capacity_per_device) >= (d_count * required_per_domain)


def validate(mission_path: str, capacity_per_device: int = 2):
    with open(mission_path, 'r', encoding='utf-8') as f:
        mission = json.load(f)

    n_devices = int(mission.get('fleet_devices', len(mission.get('units', []))))
    d_count = len(mission.get('domains', []))
    required = int(mission.get('required_active_per_domain', 1))

    ok = feasible(n_devices, d_count, capacity_per_device, required)
    needed_devices = math.ceil((d_count * required) / capacity_per_device) if capacity_per_device > 0 else 10**9
    fmax = max(0, n_devices - needed_devices)

    return {
        'mission': mission_path,
        'fleet_devices': n_devices,
        'domains': d_count,
        'capacity_per_device': capacity_per_device,
        'required_active_per_domain': required,
        'feasible': ok,
        'needed_devices': needed_devices,
        'Fmax': fmax
    }


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('mission')
    ap.add_argument('--capacity', type=int, default=2)
    args = ap.parse_args()

    result = validate(args.mission, capacity_per_device=args.capacity)
    print(json.dumps(result, indent=2))
