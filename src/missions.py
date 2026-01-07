
# src/missions.py
import json
import os

def load_mission(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_default_mission() -> dict:
    # Choose a default (fleet12)
    return load_mission(os.path.join("missions", "mission_fleet12_deadline_ms1.json"))
