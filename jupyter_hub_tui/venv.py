# Resolve environment for venv detection.

import os
from pathlib import Path


def is_active() -> bool:
    # Check if a venv is active in the current process environment.
    return bool(os.environ.get("VIRTUAL_ENV"))


def active_path() -> str | None:
    # Return the path to the active venv, or None.
    return os.environ.get("VIRTUAL_ENV")


def local_exists(config_path: str) -> bool:
    # Check if the configured local venv path exists on disk.
    if not config_path:
        return False
    return Path(os.path.expanduser(config_path)).exists()
