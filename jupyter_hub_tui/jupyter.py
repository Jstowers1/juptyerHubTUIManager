# Jupyter notebook integration.
# Lists notebooks on remote, opens them in euporie inside the PTY.

from .ssh_manager import SSHManager


def tunnel_command(ssh: SSHManager, node_name: str, port: int = 8888,
                   remote_venv: str = "~/.venv") -> list[str]:
    # SSH command that starts jupyter on remote with port forwarding.
    if node_name not in ssh.nodes:
        raise ValueError(f"Unknown node: {node_name}")
    remote_cmd = (
        f"source {remote_venv}/bin/activate 2>/dev/null; "
        f"jupyter lab --no-browser --port={port} --ServerApp.token=''"
    )
    return ssh.raw_ssh_command(node_name) + [
        "-L", f"{port}:localhost:{port}",
        remote_cmd,
    ]


def euporie_cmd(notebook_path: str | None = None) -> list[str]:
    # Launch euporie in the embedded PTY. Optionally open a notebook.
    cmd = ["euporie", "notebook"]
    if notebook_path:
        cmd.append(notebook_path)
    return cmd
