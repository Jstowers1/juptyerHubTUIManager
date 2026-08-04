# SSH connection manager. Tracks which node is active, builds connect commands.

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
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

    def raw_ssh_command(self, name: str) -> list[str]:
        # Plain ssh without kitty wrapper. For embedded terminal.
        # ControlMaster lets _ssh_prefix commands reuse this connection.
        node = self._nodes[name]
        cmd = [
            "ssh", "-tt",
            "-o", "ControlMaster=auto",
            "-o", "ControlPath=/tmp/tui-ssh-%C",
            "-o", "ControlPersist=60",
            f"{node.user}@{node.host}", "-p", str(node.port),
        ]
        if node.proxy and node.proxy in self._nodes:
            proxy = self._nodes[node.proxy]
            cmd += ["-J", f"{proxy.user}@{proxy.host}:{proxy.port}"]
        return cmd

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

    def launch(self, name: str) -> list[str]:
        # Return the raw SSH command for the embedded terminal to run.
        return self.raw_ssh_command(name)

    def _ssh_prefix(self, name: str) -> list[str]:
        # Build ssh + proxy prefix for remote commands.
        # Uses same ControlPath as raw_ssh_command so commands reuse
        # the interactive SSH session without re-authenticating.
        node = self._nodes[name]
        cmd = ["ssh"]
        if node.proxy and node.proxy in self._nodes:
            proxy = self._nodes[node.proxy]
            cmd += ["-J", f"{proxy.user}@{proxy.host}:{proxy.port}"]
        cmd += [
            "-o", "ControlPath=/tmp/tui-ssh-%C",
            "-o", "ConnectTimeout=5",
            "-p", str(node.port), f"{node.user}@{node.host}",
        ]
        return cmd

    def list_remote_dir(self, name: str, path: str = "~") -> list[dict]:
        # List a directory on a remote node. Returns list of {name, is_dir}.
        # ponytail: blocking subprocess, ~0.5s per call. Fine for browsing.
        cmd = self._ssh_prefix(name) + [
            "-o", "ConnectTimeout=5",
            f"ls -1F {path} 2>/dev/null",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        entries = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            is_dir = line.endswith("/")
            entries.append({"name": line.rstrip("/*"), "is_dir": is_dir})
        entries.sort(key=lambda e: (not e["is_dir"], e["name"]))
        return entries

    def remote_git_status(self, name: str, path: str) -> str | None:
        # Run git status on remote. Returns porcelain output or None.
        cmd = self._ssh_prefix(name) + [
            "-o", "ConnectTimeout=5",
            f"git -C {path} status --porcelain=v1 -b 2>/dev/null",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def remote_git_branches(self, name: str, path: str) -> list[str]:
        # List branches on remote. Returns [branch_name, ...].
        cmd = self._ssh_prefix(name) + [
            "-o", "ConnectTimeout=5",
            f"git -C {path} branch --format='%(refname:short)' 2>/dev/null",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []
        if result.returncode != 0:
            return []
        return [b.strip().strip("'") for b in result.stdout.strip().splitlines() if b.strip()]

    def remote_git_checkout(self, name: str, path: str, branch: str) -> bool:
        # Checkout a branch on remote. Returns True on success.
        cmd = self._ssh_prefix(name) + [
            "-o", "ConnectTimeout=5",
            f"git -C {path} checkout {branch} 2>&1",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return result.returncode == 0

    def setup_keys_command(self) -> list[str]:
        # Return a shell command that generates a key and copies it to all nodes.
        # Runs in the embedded terminal so the user can enter passwords.
        targets = []
        for name, node in self._nodes.items():
            targets.append(f"ssh-copy-id -p {node.port} {node.user}@{node.host}")
        script = (
            "test -f ~/.ssh/id_ed25519 || "
            "ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ''; "
            + "; ".join(targets)
        )
        return ["bash", "-c", script]

    def setup_keys(self, name: str | None = None) -> list[str]:
        # Generate an SSH key if none exists, then copy to target nodes.
        # Returns a list of status messages for display.
        messages: list[str] = []
        key_path = Path(os.path.expanduser("~/.ssh/id_ed25519"))
        if not key_path.exists():
            subprocess.run(
                ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", ""],
                check=True,
            )
            messages.append("Generated ed25519 key.")
        else:
            messages.append("Key exists, skipping generation.")
        targets = [name] if name else list(self._nodes.keys())
        for n in targets:
            if n not in self._nodes:
                continue
            node = self._nodes[n]
            result = subprocess.run(
                ["ssh-copy-id", "-p", str(node.port)]
                + [f"{node.user}@{node.host}"],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                messages.append(f"Key copied to {n}.")
            else:
                messages.append(f"Failed on {n}: {result.stderr.strip()}")
        return messages


def _base_ssh_command() -> list[str]:
    # Use kitty +kitten ssh if kitty is the parent terminal.
    if os.environ.get("KITTY_WINDOW_ID") or os.environ.get("KITTY_PID"):
        return ["kitty", "+kitten", "ssh"]
    return ["ssh"]


def _self_check() -> None:
    data = {
        "nodes": {
            "login": {
                "host": "login.example.org",
                "user": "test",
                "port": 22,
                "description": "test",
            },
            "worker-1": {
                "host": "worker-1.example.org",
                "user": "test",
                "port": 22,
                "description": "test",
                "proxy": "login",
    }
        }
    }
    mgr = SSHManager(data)
    assert "login" in mgr.nodes
    assert mgr.active is None
    pub = mgr.set_active("login")
    assert pub.name == "login"
    assert mgr.active is not None
    assert mgr.active.name == "login"
    cmd = mgr.command("worker-1")
    assert "-J" in cmd, f"proxy jump missing: {cmd}"
    raw = mgr.raw_ssh_command("worker-1")
    assert raw[0] == "ssh", f"raw command should start with ssh: {raw}"
    assert "-J" in raw, f"proxy jump missing from raw: {raw}"
    assert "ControlMaster" in " ".join(raw), "ControlMaster missing"
    print("SSH self-check passed")


if __name__ == "__main__":
    _self_check()
