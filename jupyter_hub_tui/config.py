#Load config from config.json. Search order:
#1. ~/.config/jhtui/config.json (for installed binary)
#2. config.json next to the package (dev checkout)
#3. config.example.json (template fallback)

import json
import os
from pathlib import Path
from typing import Any

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_USER_CONFIG = Path.home() / ".config" / "jhtui" / "config.json"
_REPO_CONFIG = _PACKAGE_DIR / "config.json"
_EXAMPLE_FILE = _PACKAGE_DIR / "jupyter_hub_tui" / "config.example.json"


def _find_config() -> Path:
    if _USER_CONFIG.exists():
        return _USER_CONFIG
    if _REPO_CONFIG.exists():
        return _REPO_CONFIG
    return _EXAMPLE_FILE


class ConfigError(Exception):
    pass


def load() -> dict[str, Any]:
    path = _find_config()
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        #No config anywhere. Start with an empty template.
        return {"nodes": {}, "_example": True}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Bad JSON in {path}: {exc}") from exc
    if path == _EXAMPLE_FILE:
        data["_example"] = True
    return data


def config_path() -> Path:
    #Path of the loaded config file.
    return _find_config()


def init_user_config() -> Path:
    #Copy the example template to ~/.config/jhtui/config.json.
    if _USER_CONFIG.exists():
        return _USER_CONFIG
    _USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    import shutil
    shutil.copy(_EXAMPLE_FILE, _USER_CONFIG)
    return _USER_CONFIG


def nodes(data: dict[str, Any]) -> dict[str, dict]:
    return data.get("nodes", {})


def venv_activate_cmd(data: dict[str, Any]) -> str:
    return data.get("venv", {}).get("activate_cmd", "")


def venv_pythonpath(data: dict[str, Any]) -> str:
    return data.get("venv", {}).get("pythonpath", "")


def git_repo_path(data: dict[str, Any]) -> str:
    return data.get("git", {}).get("repo_path", ".")


def browse_path(data: dict[str, Any]) -> str:
    return data.get("browse_path", "~")


def save(data: dict[str, Any]) -> None:
    #Write config. Strip internal keys first. Writes to the
    #same path load() reads from. For the example template,
    #create ~/.config/jhtui/config.json instead.
    clean = {k: v for k, v in data.items() if not k.startswith("_")}
    path = _find_config()
    if path == _EXAMPLE_FILE:
        path = _USER_CONFIG
        path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(clean, f, indent=2)


def update_node(data: dict[str, Any], name: str, **fields: Any) -> dict[str, Any]:
    #Update one node in the data dict. Return the updated dict.
    if "nodes" not in data:
        data["nodes"] = {}
    if name not in data["nodes"]:
        data["nodes"][name] = {}
    data["nodes"][name].update(fields)
    return data


def set_git_repo_path(data: dict[str, Any], path: str) -> dict[str, Any]:
    #Set the git repo path in the data dict. Return the updated dict.
    if "git" not in data:
        data["git"] = {}
    data["git"]["repo_path"] = path
    return data
