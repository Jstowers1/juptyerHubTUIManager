#Entry point for the jhtui binary. Asks for the key passphrase once,
#before the TUI takes the screen.

from __future__ import annotations

import os
import subprocess


def _spawn_agent() -> None:
    #Start a fresh agent and export its socket.
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


def main() -> None:
    #Spawn an agent only when no socket is exported.
    if not os.environ.get("SSH_AUTH_SOCK"):
        _spawn_agent()
    key = os.path.expanduser("~/.ssh/id_ed25519")
    if os.path.exists(key):
        #Inherit the terminal. ssh-add asks for the passphrase here.
        r = subprocess.run(["ssh-add", key])
        if r.returncode == 2:
            #Socket is dead. Fresh agent, ask once more.
            _spawn_agent()
            subprocess.run(["ssh-add", key])
    from .app import main as app_main

    app_main()


if __name__ == "__main__":
    main()
