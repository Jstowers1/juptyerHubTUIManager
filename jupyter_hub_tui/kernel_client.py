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
        # Check kernel process is still running, not a network heartbeat.
        if self._kernel_proc and self._kernel_proc.poll() is not None:
            return False
        return True

    def start(self, timeout: int = 30) -> None:
        # Start remote kernel, read connection file, set up tunnels.
        self._ssh.wait_for_master(self._node)
        self._launch_remote_kernel()
        self._read_connection_file()
        self._start_tunnels()
        self._connect_client(timeout)
        # Set matplotlib to Agg so plots go to PNG buffers.
        # Drain the response so it doesn't clog the next execute().
        import queue as _queue
        self._kc.execute(
            "import matplotlib;matplotlib.use('Agg')", silent=True, store_history=False
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self._kc.get_iopub_msg(timeout=2)
            except _queue.Empty:
                break

    def _launch_remote_kernel(self) -> None:
        # SSH exec that starts ipykernel on remote. Blocks (kernel runs).
        parts = [self._venv_cmd] if self._venv_cmd else []
        # PS1 bypasses .bashrc non-interactive guard so conda functions load.
        prefix = ("PS1='$ ' " + " && ".join(parts) + " && ") if parts else ""
        self._conn_file = f"/tmp/jhtui-kernel-{int(time.time() * 1000)}.json"
        self._stderr_file = f"/tmp/jhtui-kernel-stderr-{int(time.time() * 1000)}.log"
        env_prefix = f"PYTHONPATH={self._pythonpath} " if self._pythonpath else ""
        launcher = (
            prefix
            + env_prefix
            + f"python -m ipykernel_launcher --ip=127.0.0.1 -f {self._conn_file}"
            + f" 2>{self._stderr_file}"
        )
        cmd = self._ssh_cmd() + [launcher]
        import logging
        logging.basicConfig(filename="/tmp/jhtui-debug.log", level=logging.DEBUG)
        logging.debug("kernel launch cmd: %s", cmd)
        logging.debug("launcher: %s", launcher)
        self._kernel_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def _ssh_cmd(self) -> list[str]:
        # Reuse the interactive terminal's ControlMaster socket.
        return self._ssh._ssh_prefix(self._node)

    def _read_remote_stderr(self) -> str:
        cmd = self._ssh_cmd() + [f"cat {self._stderr_file}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            return result.stdout if result.returncode == 0 else ""
        except Exception:
            return ""

    def _read_connection_file(self, retries: int = 40) -> None:
        # Fail fast if kernel already died.
        if self._kernel_proc and self._kernel_proc.poll() is not None:
            err = self._read_remote_stderr()
            # Also read SSH stderr/stdout for the real error.
            ssh_out = self._kernel_proc.stdout.read(4096).decode(errors="replace") if self._kernel_proc.stdout else ""
            ssh_err = self._kernel_proc.stderr.read(4096).decode(errors="replace") if self._kernel_proc.stderr else ""
            import logging
            logging.debug("kernel proc exited code=%s", self._kernel_proc.returncode)
            logging.debug("kernel stdout: %s", ssh_out)
            logging.debug("kernel stderr: %s", ssh_err)
            logging.debug("remote stderr: %s", err)
            raise RuntimeError(
                f"kernel exited (code {self._kernel_proc.returncode})"
                f"\nssh out: {ssh_out}"
                f"\nssh err: {ssh_err}"
                f"\nremote stderr: {err}"
            )
        # Single SSH command that polls on the remote side.
        waiter = (
            f"for i in $(seq 1 {retries}); do "
            f"if [ -f {self._conn_file} ]; then cat {self._conn_file}; exit 0; fi; "
            f"sleep 0.5; "
            f"done; exit 1"
        )
        cmd = self._ssh_cmd() + [waiter]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=retries * 0.5 + 10
            )
        except subprocess.TimeoutExpired:
            err = self._read_remote_stderr()
            raise RuntimeError(
                f"connection file wait timed out: {self._conn_file}"
                + (f"\nremote stderr:\n{err}" if err else "")
            )
        if result.returncode == 0 and result.stdout.strip():
            try:
                self._conn_info = json.loads(result.stdout)
                return
            except json.JSONDecodeError:
                pass
        # Poll exhausted without getting the file. Capture all diagnostics.
        err = self._read_remote_stderr()
        kproc_out = ""
        kproc_err = ""
        if self._kernel_proc and self._kernel_proc.poll() is not None:
            kproc_out = self._kernel_proc.stdout.read(4096).decode(errors="replace") if self._kernel_proc.stdout else ""
            kproc_err = self._kernel_proc.stderr.read(4096).decode(errors="replace") if self._kernel_proc.stderr else ""
        import logging
        logging.debug("poll exhausted. ssh rc=%s", result.returncode)
        logging.debug("poll stderr: %s", result.stderr)
        logging.debug("kernel proc poll: %s", self._kernel_proc.poll() if self._kernel_proc else None)
        logging.debug("remote stderr: %s", err)
        logging.debug("kproc stdout: %s", kproc_out)
        logging.debug("kproc stderr: %s", kproc_err)
        raise RuntimeError(
            f"connection file not ready after {retries} retries: {self._conn_file}"
            f"\nremote stderr: {err}"
            f"\nkernel out: {kproc_out}"
            f"\nkernel err: {kproc_err}"
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
            "-N",
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
            stderr=subprocess.DEVNULL,
        )
        # Wait for tunnels to establish.
        time.sleep(1.0)
        if self._tunnel_proc.poll() is not None:
            raise RuntimeError("SSH tunnel process exited immediately")

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
        import queue as _queue
        if self._kc is None:
            return CellResult(error="kernel not started")
        # Drain stale iopub messages from prior commands.
        while True:
            try:
                self._kc.get_iopub_msg(timeout=0.1)
            except _queue.Empty:
                break
            except Exception:
                break
        msg_id = self._kc.execute(code, store_history=True)
        result = CellResult()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self._kc.get_iopub_msg(timeout=2)
            except _queue.Empty:
                continue
            except Exception as e:
                # Real socket error, not just timeout.
                if "transport" in str(e).lower() or "socket" in str(e).lower():
                    result.error = f"kernel connection lost: {e}"
                    break
                continue
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
        else:
            result.error = f"execution timed out after {timeout}s"
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
