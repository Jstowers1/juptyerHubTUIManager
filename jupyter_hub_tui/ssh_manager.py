# SSH connection manager. Tracks which node is active, builds connect commands.

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any

from . import config as cfg


def _control_path(name: str) -> str:
    # Per-node socket path so each node gets its own master.
    return f"/tmp/tui-ssh-{name}-%C"


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
        # Plain ssh for embedded terminal. ssh-agent started by jhtui
        # launcher handles passphrase (one per session).
        node = self._nodes[name]
        cmd = [
            "ssh", "-tt",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={_control_path(name)}",
            "-o", "ControlPersist=no",
            f"{node.user}@{node.host}", "-p", str(node.port),
        ]
        if node.proxy and node.proxy in self._nodes:
            proxy = self._nodes[node.proxy]
            cmd += ["-o", f"ProxyJump={proxy.user}@{proxy.host}:{proxy.port}"]
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
            cmd += ["-o", f"ProxyJump={proxy.user}@{proxy.host}:{proxy.port}"]
        cmd += [
            "-o", f"ControlPath={_control_path(name)}",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            "-p", str(node.port), f"{node.user}@{node.host}",
        ]
        return cmd

    def list_remote_dir(self, name: str, path: str = "~") -> list[dict]:
        cmd = self._ssh_prefix(name) + [
            "-o", "ConnectTimeout=5",
            f"ls -1F {shlex.quote(path)} 2>/dev/null",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except subprocess.TimeoutExpired:
            return []
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
            f"git -C {shlex.quote(path)} status --porcelain=v1 -b 2>/dev/null",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    def remote_git_log(self, name: str, path: str, count: int = 10) -> str:
        # Return recent commit log from remote.
        cmd = self._ssh_prefix(name) + [
            "-o", "ConnectTimeout=5",
            f"git -C {shlex.quote(path)} log --oneline -{count} 2>/dev/null",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""
        return result.stdout.strip()

    def remote_git_fetch(self, name: str, path: str) -> bool:
        cmd = self._ssh_prefix(name) + [
            "-o", "ConnectTimeout=10",
            f"git -C {shlex.quote(path)} fetch 2>&1",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return result.returncode == 0

    def remote_git_pull(self, name: str, path: str) -> tuple[bool, str]:
        cmd = self._ssh_prefix(name) + [
            "-o", "ConnectTimeout=10",
            f"git -C {shlex.quote(path)} pull 2>&1",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False, "timeout"
        return result.returncode == 0, result.stdout.strip()

    def remote_git_diff(self, name: str, path: str) -> str:
        cmd = self._ssh_prefix(name) + [
            "-o", "ConnectTimeout=5",
            f"git -C {shlex.quote(path)} diff 2>/dev/null",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""
        return result.stdout.strip()

    def remote_git_branches(self, name: str, path: str) -> list[str]:
        # List branches on remote. Returns [branch_name, ...].
        cmd = self._ssh_prefix(name) + [
            "-o", "ConnectTimeout=5",
            f"git -C {shlex.quote(path)} branch --format='%(refname:short)' 2>/dev/null",
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
            f"git -C {shlex.quote(path)} checkout {shlex.quote(branch)} 2>&1",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return result.returncode == 0

    def remote_venv_active(self, name: str, venv_path: str) -> bool:
        # Check if remote venv exists.
        if not venv_path:
            return False
        cmd = self._ssh_prefix(name) + [
            f"test -d {shlex.quote(venv_path)}/bin && echo yes 2>/dev/null",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return "yes" in result.stdout

    def scp_file(self, name: str, remote_path: str, local_path: str) -> bool:
        # Download a file from remote to local via scp.
        node = self._nodes[name]
        scp_args = ["scp"]
        if node.proxy and node.proxy in self._nodes:
            proxy = self._nodes[node.proxy]
            scp_args += ["-o", f"ProxyJump={proxy.user}@{proxy.host}:{proxy.port}"]
        scp_args += [
            "-o", f"ControlPath={_control_path(name)}",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            "-P", str(node.port),
            f"{node.user}@{node.host}:{remote_path}",
            local_path,
        ]
        try:
            result = subprocess.run(scp_args, capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return result.returncode == 0

    def scp_upload(self, name: str, local_path: str, remote_path: str) -> bool:
        # Upload a file from local to remote via scp.
        node = self._nodes[name]
        scp_args = ["scp"]
        if node.proxy and node.proxy in self._nodes:
            proxy = self._nodes[node.proxy]
            scp_args += ["-o", f"ProxyJump={proxy.user}@{proxy.host}:{proxy.port}"]
        scp_args += [
            "-o", f"ControlPath={_control_path(name)}",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            "-P", str(node.port),
            local_path,
            f"{node.user}@{node.host}:{remote_path}",
        ]
        try:
            result = subprocess.run(scp_args, capture_output=True, text=True, timeout=15)
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
    assert raw[0] == "ssh", f"raw should start with ssh: {raw}"
    assert "ProxyJump" in " ".join(raw), "ProxyJump missing from raw"
    assert "ControlMaster" in " ".join(raw), "ControlMaster missing"
    assert "tui-ssh-worker-1" in " ".join(raw), "per-node ControlPath missing"
    assert "ControlPersist=no" in " ".join(raw), "ControlPersist should be no"
    assert hasattr(mgr, "scp_file"), "scp_file missing"
    assert hasattr(mgr, "scp_upload"), "scp_upload missing"
    assert not hasattr(mgr, "setup_keys"), "blocking setup_keys should be deleted"
    print("SSH self-check passed")


if __name__ == "__main__":
    _self_check()
