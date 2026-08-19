# Remote IPython kernel client over SSH tunnels.
# Starts a kernel on the remote, reads its connection file,
# forwards the ZMQ ports via SSH, then talks jupyter_client locally.

from __future__ import annotations

import json
import os
import re
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
        self._forward_cmd: list[str] | None = None
        self._kernel_pid: int | None = None
        self._conn_info: dict = {}

    @property
    def alive(self) -> bool:
        if self._kc is None:
            return False
        # Detached kernel: ask jupyter_client (heartbeat-based).
        return bool(self._kc.is_alive())

    def start(self, timeout: int = 30) -> None:
        # Start remote kernel, read connection file, set up tunnels.
        self._launch_remote_kernel()
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
        # One short SSH session: start detached kernel, wait for conn file.
        # Detached = no held mux session for kernel lifetime (MaxSessions).
        parts = [self._venv_cmd] if self._venv_cmd else []
        # PS1 bypasses .bashrc non-interactive guard so conda functions load.
        prefix = ("PS1='$ ' " + " && ".join(parts) + " && ") if parts else ""
        ts = int(time.time() * 1000)
        self._conn_file = f"/tmp/jhtui-kernel-{ts}.json"
        self._stderr_file = f"/tmp/jhtui-kernel-stderr-{ts}.log"
        env_prefix = f"PYTHONPATH={self._pythonpath} " if self._pythonpath else ""
        launcher = (
            # Parenthesize: exec makes the subshell BECOME python, so $!
            # is the real kernel pid. All activate errors land in EF.
            f"( {prefix}{env_prefix}exec nohup python -m ipykernel_launcher --ip=127.0.0.1 -f {self._conn_file} )"
            f" < /dev/null >{self._stderr_file} 2>&1 &"
            + f" kernel_pid=$!;"
            + f" for i in $(seq 1 140); do"
            + f"   [ -s {self._conn_file} ] && break;"
            + f"   kill -0 $kernel_pid 2>/dev/null || {{ echo KERNEL_DIED; cat {self._stderr_file}; exit 1; }};"
            + f"   sleep 0.5;"
            + f" done;"
            + f" [ -s {self._conn_file} ] || {{ echo KERNEL_TIMEOUT; cat {self._stderr_file}; exit 1; }};"
            + f" echo KERNEL_PID=$kernel_pid;"
            + f" cat {self._conn_file}"
        )
        cmd = self._ssh_cmd() + [launcher]
        try:
            result = subprocess.run(
                cmd, capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=90
            )
        except subprocess.TimeoutExpired as e:
            partial = (e.stdout or "")[:500]
            raise RuntimeError(
                f"kernel launch timed out after 90s (slow node or conda?)"
                f"\npartial output:\n{partial}"
            ) from e
        if result.returncode != 0 or "KERNEL_DIED" in result.stdout:
            raise RuntimeError(
                f"kernel launch failed (code {result.returncode})"
                f"\n{result.stdout}{result.stderr}"
            )
        # PID line precedes the JSON; venv noise may sit between.
        m = re.search(r"^KERNEL_PID=(\d+)", result.stdout, re.M)
        if not m:
            raise RuntimeError(f"no KERNEL_PID in output:\n{result.stdout[:500]}")
        self._kernel_pid = int(m.group(1))
        try:
            # Brace-find: conda/venv activation noise may precede the JSON.
            start = result.stdout.find("{")
            end = result.stdout.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("no JSON object in output")
            self._conn_info = json.loads(result.stdout[start : end + 1])
        except (json.JSONDecodeError, ValueError) as e:
            raise RuntimeError(
                f"bad connection file:\n{result.stdout[:500]}"
            ) from e

    def _read_connection_file(self, retries: int = 40) -> None:
        # Kept for shutdown-restart parity; start() no longer calls it.
        raise RuntimeError("connection file already read at launch")

    def _ssh_cmd(self) -> list[str]:
        # Reuse the interactive terminal's ControlMaster socket.
        # One auth at startup; kernel sessions multiplex over it.
        return self._ssh._ssh_prefix(self._node)

    def _read_remote_stderr(self) -> str:
        cmd = self._ssh_cmd() + [f"cat {self._stderr_file}"]
        try:
            result = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=5)
            return result.stdout if result.returncode == 0 else ""
        except Exception:
            return ""

    def _start_tunnels(self) -> None:
        # Add -L forwards to the running interactive master via -O forward.
        # Zero extra SSH connections; the master already exists.
        ports = [
            self._conn_info["shell_port"],
            self._conn_info["iopub_port"],
            self._conn_info["stdin_port"],
            self._conn_info["control_port"],
            self._conn_info["hb_port"],
        ]
        node = self._ssh.nodes[self._node]
        cp = self._ssh._control_path(self._node)
        cmd = ["ssh", "-o", f"ControlPath={cp}"]
        for p in ports:
            cmd += ["-L", f"{p}:127.0.0.1:{p}"]
        if node.proxy and node.proxy in self._ssh.nodes:
            proxy = self._ssh.nodes[node.proxy]
            cmd += ["-o", f"ProxyJump={proxy.user}@{proxy.host}:{proxy.port}"]
        cmd += ["-O", "forward", "-p", str(node.port), f"{node.user}@{node.host}"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=10
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError("tunnel setup timed out") from e
        if result.returncode != 0:
            hint = (
                "No live SSH master. Reconnect the terminal to this node, "
                "then reopen the notebook."
            )
            if "mux" in result.stderr.lower() or "control" in result.stderr.lower():
                hint = "Stale ControlSocket removed. Reopen the notebook to retry."
            raise RuntimeError(f"tunnel setup failed: {result.stderr.strip()}\n{hint}")
        self._forward_cmd = cmd

    def _stop_tunnels(self) -> None:
        # Remove the forwards with -O cancel (master stays up).
        if self._forward_cmd is None:
            return
        cancel = self._forward_cmd[:]
        cancel[cancel.index("forward")] = "cancel"
        try:
            subprocess.run(cancel, capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
        except Exception:
            pass
        self._forward_cmd = None

    def _connect_client(self, timeout: int) -> None:
        kc = BlockingKernelClient()
        kc.load_connection_info(self._conn_info)
        kc.start_channels()
        try:
            kc.wait_for_ready(timeout=timeout)
        except Exception as e:
            kc.stop_channels()
            raise RuntimeError(
                f"kernel not ready: {e}"
                + f"\n{self._diagnose_dead_kernel()}"
            ) from e
        self._kc = kc

    def _diagnose_dead_kernel(self) -> str:
        # One short SSH session: pid alive? stderr? Kill the orphan.
        pid = self._kernel_pid
        probe = f"kill -0 {pid} 2>/dev/null && echo ALIVE || echo DEAD"
        parts = [probe]
        if self._stderr_file:
            parts.append(f"echo ---; tail -20 {self._stderr_file}")
            parts.append(f"kill -9 {pid} 2>/dev/null")
        try:
            result = subprocess.run(
                self._ssh_cmd() + ["; ".join(parts)],
                capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=15,
            )
            return f"kernel pid {pid}: {result.stdout.strip()}"
        except Exception:
            return "(diagnosis failed)"

    def execute(self, code: str, timeout: int = 120) -> CellResult:
        # Execute code, collect all output until idle.
        import queue as _queue
        if self._kc is None:
            return CellResult(error="kernel not started")
        # Drain stale iopub messages.
        while True:
            try:
                self._kc.get_iopub_msg(timeout=0.01)
            except _queue.Empty:
                break
        msg_id = self._kc.execute(code, store_history=True)
        result = CellResult()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                msg = self._kc.get_iopub_msg(timeout=2)
            except _queue.Empty:
                # Check shell channel for idle reply.
                try:
                    reply = self._kc.get_shell_msg(timeout=0.1)
                    if reply.get("parent_header", {}).get("msg_id") == msg_id:
                        break
                except _queue.Empty:
                    pass
                continue
            except Exception as e:
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
        self._stop_tunnels()
        # Detached kernel: kill pid, clean files via one short session.
        if self._conn_file:
            kill = f"kill -9 {self._kernel_pid} 2>/dev/null; " if self._kernel_pid else ""
            try:
                subprocess.run(
                    self._ssh_cmd()
                    + [kill + f"rm -f {self._conn_file} {self._stderr_file}"],
                    capture_output=True, stdin=subprocess.DEVNULL, timeout=10,
                )
            except Exception:
                pass


def _self_check() -> None:
    # Verify class structure without a real SSH connection.
    rk = RemoteKernel.__new__(RemoteKernel)
    rk._kc = None
    rk._forward_cmd = None
    rk._kernel_pid = None
    rk._conn_info = {}
    rk._conn_file = ""
    rk._stderr_file = ""
    assert not rk.alive, "alive should be False with no kernel"
    assert RemoteKernel.execute.__name__ == "execute"
    assert CellResult().images == []
    assert CellResult().error is None
    assert CellResult().stdout == ""
    print("kernel_client self-check passed")


if __name__ == "__main__":
    _self_check()
