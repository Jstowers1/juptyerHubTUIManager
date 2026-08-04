# SSH connection manager. Tracks which node is active, builds connect commands.

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from . import config as cfg


@dataclass
class Node:
    name: str
    host: str
    user: str
    port: int
    description: str
    proxy: str | None = None
    connected: bool = False


class SSHManager:
    # Wraps ssh subprocess management. Does not hold persistent connections.

    def __init__(self, data: dict[str, Any]):
        self._nodes: dict[str, Node] = {}
        for name, info in cfg.nodes(data).items():
            self._nodes[name] = Node(
                name=name,
                host=info["host"],
                user=info.get("user", os.environ.get("USER", "")),
                port=info.get("port", 22),
                description=info.get("description", ""),
                proxy=info.get("proxy"),
            )
        self._active: str | None = None

    @property
    def nodes(self) -> dict[str, Node]:
        return self._nodes

    @property
    def active(self) -> Node | None:
        if self._active:
            return self._nodes[self._active]
        return None

    def set_active(self, name: str) -> Node:
        # Mark a node as the active connection target.
        node = self._nodes[name]
        self._active = name
        return node

    def command(self, name: str) -> list[str]:
        # Build the ssh command for a node. Uses kitty +kitten ssh when available
        # to copy terminfo and enable graphics over the connection.
        node = self._nodes[name]
        cmd = _base_ssh_command()
        cmd += [
            f"{node.user}@{node.host}",
            "-p",
            str(node.port),
        ]
        if node.proxy and node.proxy in self._nodes:
            proxy = self._nodes[node.proxy]
            cmd += [
                "-J",
                f"{proxy.user}@{proxy.host}:{proxy.port}",
            ]
        return cmd

    def command_str(self, name: str) -> str:
        return " ".join(self.command(name))

    def launch(self, name: str) -> None:
        # Launch an interactive ssh session in a new terminal.
        cmd = self.command(name)
        subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


def _base_ssh_command() -> list[str]:
    # Use kitty +kitten ssh if kitty is the parent terminal.
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("KITTY_PID"):
        return ["kitty", "+kitten", "ssh"]
    return ["ssh"]


def _self_check() -> None:
    data = {
        "nodes": {
            "pub": {
                "host": "pub.example.edu",
                "user": "test",
                "port": 22,
                "description": "test",
            },
            "cobalt": {
                "host": "cobalt.icecube.example.edu",
                "user": "test",
                "port": 22,
                "description": "test",
                "proxy": "pub",
    }
        }
    }
    mgr = SSHManager(data)
    assert "pub" in mgr.nodes
    assert mgr.active is None
    pub = mgr.set_active("pub")
    assert pub.name == "pub"
    assert mgr.active is not None
    assert mgr.active.name == "pub"
    cmd = mgr.command("cobalt")
    assert "-J" in cmd, f"proxy jump missing: {cmd}"
    print("SSH self-check passed")


if __name__ == "__main__":
    _self_check()
