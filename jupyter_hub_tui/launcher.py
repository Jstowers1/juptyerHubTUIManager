#Entry point for the jhtui binary. Starts ssh-agent first so the
#key passphrase is entered once per session.

from __future__ import annotations

import atexit
import os
import signal
import subprocess


def _agent_alive() -> bool:
    #ssh-add -l is the ssh-native agent probe. Exit 2 = no agent.
    r = subprocess.run(
        ["ssh-add", "-l"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
    return r.returncode != 2


def _start_agent() -> None:
    #Spawn a fresh agent only when the current one is unreachable.
    if not _agent_alive():
        out = subprocess.run(
            ["ssh-agent", "-s"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
        #Agent output is shell syntax: VAR=val; export VAR;
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
        subprocess.run(["ssh-add", key], timeout=60)


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
