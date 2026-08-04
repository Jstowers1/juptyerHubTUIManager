# Load config from config.json, fall back to example template.

import json
import os
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _CONFIG_DIR / "config.json"
_EXAMPLE_FILE = _CONFIG_DIR / "config.example.json"


class ConfigError(Exception):
    pass


def load() -> dict[str, Any]:
    path = _CONFIG_FILE if _CONFIG_FILE.exists() else _EXAMPLE_FILE
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Bad JSON in {path.name}: {exc}") from exc
    if path == _EXAMPLE_FILE:
        data["_example"] = True
    return data


def nodes(data: dict[str, Any]) -> dict[str, dict]:
    return data.get("nodes", {})


def venv_path(data: dict[str, Any]) -> str:
    return data.get("venv", {}).get("path", "")


def jupyter_settings(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("jupyter", {})
