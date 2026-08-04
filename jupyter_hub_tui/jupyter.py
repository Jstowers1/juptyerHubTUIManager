# Jupyter notebook integration via euporie.
# Tunnels remote jupyter port to localhost, launches euporie notebook.

import subprocess

from .ssh_manager import SSHManager


def launch(ssh: SSHManager, node_name: str, port: int = 8888,
           remote_venv: str = "~/.venv/icetop") -> None:
    # Start jupyter on the remote via SSH tunnel, then launch euporie.
    if node_name not in ssh.nodes:
        raise ValueError(f"Unknown node: {node_name}")
    remote_cmd = (
        f"source {remote_venv}/bin/activate 2>/dev/null; "
        f"jupyter lab --no-browser --port={port} --ServerApp.token=''"
    )
    # Run jupyter in background on remote, forward port via -L tunnel.
    tunnel_cmd = ssh.raw_ssh_command(node_name) + [
        "-L", f"{port}:localhost:{port}",
        remote_cmd,
    ]
    # ponytail: no error handling on Popen. If tunnel fails, euporie
    # will just not connect. Add retry when connection is flaky.
    subprocess.Popen(tunnel_cmd)
    subprocess.Popen(["euporie", "notebook"])


def open_notebook(path: str) -> None:
    subprocess.Popen(["euporie", "notebook", path])


if __name__ == "__main__":
    print("euporie available:", subprocess.run(
        ["which", "euporie"], capture_output=True, text=True
    ).stdout.strip())
