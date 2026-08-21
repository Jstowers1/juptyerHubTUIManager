#Entry point for the jhtui binary. Starts ssh-agent first so the
#key passphrase is entered once per session.

from __future__ import annotations

import atexit
import os
import shutil
import signal
import subprocess
import sys


def _start_agent() -> None:
    #Spawn ssh-agent if no agent socket exists, add the default key.
    if os.environ.get("SSH_AUTH_SOCK"):
        return
    try:
        out = subprocess.run(
            ["ssh-agent", "-s"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return
    for line in out.stdout.splitlines():
        if line.startswith("SSH_AUTH_SOCK="):
            sock = line.split("=", 1)[1].rstrip(";").strip()
            os.environ["SSH_AUTH_SOCK"] = sock
        elif line.startswith("SSH_AGENT_PID="):
            pid = line.split("=", 1)[1].rstrip(";").strip()
            os.environ["SSH_AGENT_PID"] = pid
            atexit.register(_kill_agent, pid)
    key = os.path.expanduser("~/.ssh/id_ed25519")
    if os.path.exists(key):
        #Inherit the terminal. ssh-add prompts for the passphrase here,
        #before the TUI takes over the screen.
        subprocess.run(
            ["ssh-add", key],
            timeout=30,
        )


def _kill_agent(pid: str) -> None:
    #Kill the agent this process spawned.
    try:
        os.kill(int(pid), signal.SIGTERM)
    except (ValueError, ProcessLookupError, PermissionError):
        pass


def main() -> None:
    _start_agent()
    from .app import main as app_main

    app_main()


if __name__ == "__main__":
    main()
