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


def local_venv_path(data: dict[str, Any]) -> str:
    return data.get("venv", {}).get("local_path", "")


def remote_venv_path(data: dict[str, Any]) -> str:
    return data.get("venv", {}).get("remote_path", "")


def git_repo_path(data: dict[str, Any]) -> str:
    return data.get("git", {}).get("repo_path", ".")


def jupyter_settings(data: dict[str, Any]) -> dict[str, Any]:
    return data.get("jupyter", {})


def save(data: dict[str, Any]) -> None:
    # Write config to config.json. Strips internal keys first.
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    with open(_CONFIG_FILE, "w") as f:
        json.dump(clean, f, indent=2)


def update_node(data: dict[str, Any], name: str, **fields: Any) -> dict[str, Any]:
    # Update a single node's fields in the data dict. Returns updated dict.
    if "nodes" not in data:
        data["nodes"] = {}
    if name not in data["nodes"]:
        data["nodes"][name] = {}
    data["nodes"][name].update(fields)
    return data


def set_git_repo_path(data: dict[str, Any], path: str) -> dict[str, Any]:
    # Set git repo path in the data dict. Returns updated dict.
    if "git" not in data:
        data["git"] = {}
    data["git"]["repo_path"] = path
    return data
