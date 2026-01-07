
# src/gui_sim.py
from datetime import timedelta

def format_rotation_countdown(current_tick: int, rotation_period_ticks: int) -> str:
    if rotation_period_ticks <= 0:
        return "Rotation disabled"
    remaining = rotation_period_ticks - (current_tick % rotation_period_ticks)
    if remaining == rotation_period_ticks:
        remaining = 0
    ms_left = remaining
    return f"Next rotation in {ms_left} ticks"

def format_battery_panel(battery: dict, battery_max: int) -> str:
    cells = []
    for u, val in sorted(battery.items()):
        pct = int((val / battery_max) * 100) if battery_max > 0 else 0
        cells.append(f"{u}: {val}/{battery_max} ({pct}%)")
    return " | ".join(cells)
