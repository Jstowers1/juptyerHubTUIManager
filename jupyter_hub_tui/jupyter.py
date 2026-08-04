# Jupyter notebook integration via euporie.
# Launches euporie to render notebooks in the terminal with kitty graphics support.

import subprocess

from .ssh_manager import SSHManager


def launch(ssh: SSHManager, node_name: str, port: int = 8888,
           remote_venv: str = "~/.venv/icetop") -> None:
    # Start jupyter on the remote node via SSH tunnel,
    # then launch euporie connected to the local tunnel port.
    if node_name not in ssh.nodes:
        raise ValueError(f"Unknown node: {node_name}")
    remote_cmd = (
        f"source {remote_venv}/bin/activate 2>/dev/null; "
        f"jupyter lab --no-browser --port={port} --ServerApp.token=''"
    )
    # Tunnel the remote jupyter port to localhost.
    tunnel_cmd = ssh.command(node_name) + [
        "-L", f"{port}:localhost:{port}", "-N"
    ]
    subprocess.Popen(tunnel_cmd)
    # Launch euporie to connect to the tunneled kernel.
    subprocess.Popen(["euporie", "notebooks"])


def open_notebook(path: str) -> None:
    # Open a single notebook file in euporie.
    subprocess.Popen(["euporie", "notebooks", path])


if __name__ == "__main__":
    print("euporie available:", subprocess.run(
        ["which", "euporie"], capture_output=True, text=True
    ).stdout.strip())
