# Remote IPython kernel client over SSH tunnels.
# Starts a kernel on the remote, reads its connection file,
# forwards the ZMQ ports via SSH, then talks jupyter_client locally.

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from jupyter_client import BlockingKernelClient

from .ssh_manager import SSHManager


@dataclass
class CellResult:
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    images: list[bytes] = field(default_factory=list)
    display_data: list[dict] = field(default_factory=list)


class RemoteKernel:
    # Manages a remote kernel via SSH port forwarding.

    def __init__(
        self,
        ssh: SSHManager,
        node_name: str,
        venv_cmd: str = "",
        pythonpath: str = "",
    ) -> None:
        self._ssh = ssh
        self._node = node_name
        self._venv_cmd = venv_cmd
        self._pythonpath = pythonpath
        self._kc: BlockingKernelClient | None = None
        self._tunnel_proc: subprocess.Popen | None = None
        self._kernel_proc: subprocess.Popen | None = None
        self._conn_info: dict = {}

    @property
    def alive(self) -> bool:
        if self._kc is None:
            return False
        try:
            self._kc.kernel_info(timeout=2)
            return True
        except Exception:
            return False

    def start(self, timeout: int = 30) -> None:
        # Start remote kernel, read connection file, set up tunnels.
        self._launch_remote_kernel()
        self._read_connection_file()
        self._start_tunnels()
        self._connect_client(timeout)

    def _launch_remote_kernel(self) -> None:
        # SSH exec that starts ipykernel on remote. Blocks (kernel runs).
        pp = f"PYTHONPATH={self._pythonpath}" if self._pythonpath else ""
        parts = [p for p in [self._venv_cmd, pp] if p]
        prefix = " && ".join(parts) + " && " if parts else ""
        self._conn_file = f"/tmp/jhtui-kernel-{int(time.time() * 1000)}.json"
        self._stderr_file = f"/tmp/jhtui-kernel-stderr-{int(time.time() * 1000)}.log"
        # Redirect stderr to remote file so we can read it on failure.
        launcher = (
            prefix
            + f"python -m ipykernel_launcher -f {self._conn_file}"
            + f" 2>{self._stderr_file}"
        )
        cmd = self._ssh._ssh_prefix(self._node) + [launcher]
        self._kernel_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _read_remote_stderr(self) -> str:
        cmd = self._ssh._ssh_prefix(self._node) + [f"cat {self._stderr_file}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return result.stdout if result.returncode == 0 else ""
        except Exception:
            return ""

    def _read_connection_file(self, retries: int = 40) -> None:
        # Poll the remote connection file until it exists and is valid JSON.
        import time as _time
        for _ in range(retries):
            cmd = self._ssh._ssh_prefix(self._node) + [f"cat {self._conn_file}"]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                _time.sleep(0.5)
                continue
            if result.returncode == 0 and result.stdout.strip():
                try:
                    self._conn_info = json.loads(result.stdout)
                    return
                except json.JSONDecodeError:
                    pass
            _time.sleep(0.5)
        err = self._read_remote_stderr()
        raise RuntimeError(
            f"connection file not ready after {retries} retries: {self._conn_file}"
            + (f"\nremote stderr:\n{err}" if err else "\n(no remote stderr)")
        )

    def _start_tunnels(self) -> None:
        # Forward each ZMQ port via a single SSH -L multiplexed connection.
        ports = [
            self._conn_info["shell_port"],
            self._conn_info["iopub_port"],
            self._conn_info["stdin_port"],
            self._conn_info["control_port"],
            self._conn_info["hb_port"],
        ]
        node = self._ssh.nodes[self._node]
        cp = self._ssh._control_path(self._node)
        cmd = ["ssh",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={cp}",
            "-o", "ControlPersist=120",
            "-N",  # no remote command
        ]
        for p in ports:
            cmd += ["-L", f"{p}:127.0.0.1:{p}"]
        if node.proxy and node.proxy in self._ssh.nodes:
            proxy = self._ssh.nodes[node.proxy]
            cmd += ["-o", f"ProxyJump={proxy.user}@{proxy.host}:{proxy.port}"]
        cmd += ["-p", str(node.port), f"{node.user}@{node.host}"]
        self._tunnel_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        # Wait for tunnels to establish.
        time.sleep(1.0)
        if self._tunnel_proc.poll() is not None:
            err = self._tunnel_proc.stderr.read(4096).decode()
            raise RuntimeError(f"SSH tunnel failed: {err}")

    def _connect_client(self, timeout: int) -> None:
        kc = BlockingKernelClient()
        kc.load_connection_info(self._conn_info)
        kc.start_channels()
        try:
            kc.wait_for_ready(timeout=timeout)
        except Exception as e:
            kc.stop_channels()
            err = self._read_remote_stderr()
            raise RuntimeError(
                f"kernel not ready: {e}"
                + (f"\nremote stderr:\n{err}" if err else "\n(no remote stderr)")
            ) from e
        self._kc = kc

    def execute(self, code: str, timeout: int = 60) -> CellResult:
        # Execute code, collect all output until idle.
        if self._kc is None:
            return CellResult(error="kernel not started")
        # Use matplotlib Agg so figures go to PNG buffers.
        full = (
            "import matplotlib;"
            "matplotlib.use('Agg');"
            "import matplotlib.pyplot as plt;"
            "from io import BytesIO;"
            + code
        )
        msg_id = self._kc.execute(full, user_expressions={}, store_history=True)
        result = CellResult()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self._kc.get_iopub_msg(timeout=2)
            except Exception:
                if self._kc.is_alive():
                    continue
                result.error = "kernel died"
                break
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            mtype = msg["msg_type"]
            content = msg["content"]
            if mtype == "stream":
                if content.get("name") == "stderr":
                    result.stderr += content.get("text", "")
                else:
                    result.stdout += content.get("text", "")
            elif mtype == "execute_result":
                text = content.get("data", {}).get("text/plain", "")
                if text:
                    result.stdout += text + "\n"
                data = content.get("data", {})
                if "image/png" in data:
                    import base64
                    result.images.append(base64.b64decode(data["image/png"]))
                result.display_data.append(data)
            elif mtype == "display_data":
                data = content.get("data", {})
                if "image/png" in data:
                    import base64
                    result.images.append(base64.b64decode(data["image/png"]))
                result.display_data.append(data)
            elif mtype == "error":
                tb = content.get("traceback", [])
                result.error = "\n".join(tb) if tb else content.get("evalue", "unknown error")
            elif mtype == "status" and content.get("execution_state") == "idle":
                break
        return result

    def interrupt(self) -> None:
        if self._kc is not None:
            self._kc.interrupt()

    def restart(self) -> None:
        self.shutdown()
        time.sleep(0.5)
        self.start()

    def shutdown(self) -> None:
        if self._kc is not None:
            try:
                self._kc.shutdown()
            except Exception:
                pass
            self._kc.stop_channels()
            self._kc = None
        if self._tunnel_proc is not None:
            self._tunnel_proc.terminate()
            self._tunnel_proc.wait(timeout=5)
            self._tunnel_proc = None
        if self._kernel_proc is not None:
            self._kernel_proc.terminate()
            self._kernel_proc.wait(timeout=5)
            self._kernel_proc = None


def _self_check() -> None:
    # Verify class structure without a real SSH connection.
    rk = RemoteKernel.__new__(RemoteKernel)
    rk._kc = None
    rk._tunnel_proc = None
    rk._kernel_proc = None
    rk._conn_info = {}
    assert not rk.alive, "alive should be False with no kernel"
    assert RemoteKernel.execute.__name__ == "execute"
    assert CellResult().images == []
    assert CellResult().error is None
    print("kernel_client self-check passed")


if __name__ == "__main__":
    _self_check()
