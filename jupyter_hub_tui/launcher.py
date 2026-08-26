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
    #Spawn ssh-agent if no live agent socket exists, add the default key.
    sock = os.environ.get("SSH_AUTH_SOCK", "")
    if sock and os.path.exists(sock):
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
    #Agent output is shell syntax: VAR=val; export VAR;
    #Split on semicolons first or the env var keeps the export clause.
    for line in out.stdout.splitlines():
        for tok in line.split(";"):
            tok = tok.strip()
            if tok.startswith("SSH_AUTH_SOCK="):
                os.environ["SSH_AUTH_SOCK"] = tok.split("=", 1)[1]
            elif tok.startswith("SSH_AGENT_PID="):
                pid = tok.split("=", 1)[1]
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
