# Jupyter notebook integration.
# Opens notebooks on the remote via euporie inside the SSH PTY.

from .ssh_manager import SSHManager


def notebook_cmd(ssh: SSHManager, node_name: str, notebook_path: str,
                 remote_venv: str = "~/.venv") -> list[str]:
    # SSH command that runs euporie on the remote, opening a notebook.
    # Uses the interactive PTY so graphics protocol works in kitty.
    remote_cmd = (
        f"source {remote_venv}/bin/activate 2>/dev/null; "
        f"euporie notebook {notebook_path}"
    )
    return ssh.run_in_term(node_name, remote_cmd)
