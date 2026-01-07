
# src/missions.py
import json
import os

def load_mission(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
