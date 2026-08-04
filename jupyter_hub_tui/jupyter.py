# Jupyter notebook integration via euporie.
# Runs euporie inside the embedded PTY, with SSH tunnel for the Jupyter port.

from .ssh_manager import SSHManager


def launch(ssh: SSHManager, node_name: str, port: int = 8888,
           remote_venv: str = "~/.venv") -> list[str]:
    # Return a command that starts Jupyter on remote and launches euporie.
    # The caller runs this in the embedded PTY.
    if node_name not in ssh.nodes:
        raise ValueError(f"Unknown node: {node_name}")
    remote_cmd = (
        f"source {remote_venv}/bin/activate 2>/dev/null; "
        f"jupyter lab --no-browser --port={port} --ServerApp.token=''"
    )
    # SSH tunnel: -L forwards remote port to local. Euporie connects to it.
    cmd = ssh.raw_ssh_command(node_name) + [
        "-L", f"{port}:localhost:{port}",
        remote_cmd,
    ]
    return cmd


def euporie_command(port: int = 8888) -> list[str]:
    # Return command to launch euporie in the embedded PTY.
    return ["euporie", "notebook"]
