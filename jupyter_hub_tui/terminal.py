# Embedded terminal widget for Textual.
# Runs SSH (or any command) in a PTY, renders output via pyte.

from __future__ import annotations

import os
import pty
import select
import shlex
import struct
import fcntl
import termios
from typing import Optional

import pyte
from textual.containers import VerticalScroll
from textual.reactive import reactive
from textual.widget import Widget
from textual.message import Message


class TerminalDisplay(Widget):
    # Renders a pyte screen as Textual content.

    DEFAULT_CSS = """
    TerminalDisplay {
        background: #1d1f21;
        color: #c5c8c6;
        padding: 0 1;
        overflow: hidden;
    }
    """

    class Connected(Message):
        # Sent when the PTY process exits.
        def __init__(self, exit_code: int) -> None:
            self.exit_code = exit_code
            super().__init__()

    _lines: reactive[list[str]] = reactive(list)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._screen = pyte.Screen(80, 24)
        self._stream = pyte.Stream(self._screen)
        self._master_fd: Optional[int] = None
        self._pid: Optional[int] = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, command: list[str]) -> None:
        # Fork a PTY and run the command.
        self._running = True
        self._pid, self._master_fd = pty.fork()
        if self._pid == 0:
            # Child process: exec the command.
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            os.execvpe(command[0], command, env)
        else:
            # Parent: set up non-blocking read.
            flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self.set_interval(0.05, self._poll_pty)

    def _poll_pty(self) -> None:
        # Read available PTY output and feed to pyte. Check process exit.
        if self._master_fd is None:
            return
        try:
            ready, _, _ = select.select([self._master_fd], [], [], 0)
        except (OSError, ValueError):
            return
        if ready:
            try:
                data = os.read(self._master_fd, 65536)
            except OSError:
                self._handle_exit()
                return
            if not data:
                self._handle_exit()
                return
            self._stream.feed(data.decode("utf-8", errors="replace"))
            self._refresh_display()
        # Check if child exited.
        try:
            pid, status = os.waitpid(self._pid, os.WNOHANG)
            if pid != 0:
                self._handle_exit(status)
        except ChildProcessError:
            self._handle_exit()

    def _handle_exit(self, status: int = 0) -> None:
        self._running = False
        self.set_interval(0.1, lambda: None)
        self.post_message(self.Connected(status))

    def _refresh_display(self) -> None:
        # Convert pyte screen buffer to lines for rendering.
        lines = []
        for line in self._screen.display:
            # Strip trailing whitespace but keep content.
            lines.append(line.rstrip())
        self._lines = lines
        self.refresh()

    def render(self) -> str:
        # Render current screen state as plain text.
        return "\n".join(self._lines) if self._lines else ""

    def on_key(self, event) -> None:
        # Send keystrokes to the PTY.
        if self._master_fd is None or not self._running:
            return
        key = event.key
        char = event.character
        data = _key_to_bytes(key, char)
        if data:
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass
            event.prevent_default()
            event.stop()

    def write(self, data: str) -> None:
        # Send raw data to the PTY.
        if self._master_fd is not None and self._running:
            try:
                os.write(self._master_fd, data.encode())
            except OSError:
                pass

    def stop(self) -> None:
        # Kill the PTY process.
        if self._pid is not None:
            try:
                os.kill(self._pid, 15)
            except ProcessLookupError:
                pass
        self._running = False

    def resize(self, cols: int, rows: int) -> None:
        # Resize the pyte screen and PTY.
        self._screen.resize(rows, cols)
        if self._master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)


def _key_to_bytes(key: str, char: str | None) -> bytes:
    # Map Textual key names to terminal byte sequences.
    special = {
        "enter": b"\r",
        "tab": b"\t",
        "escape": b"\x1b",
        "backspace": b"\x7f",
        "delete": b"\x1b[3~",
        "up": b"\x1b[A",
        "down": b"\x1b[B",
        "right": b"\x1b[C",
        "left": b"\x1b[D",
        "home": b"\x1b[H",
        "end": b"\x1b[F",
        "pageup": b"\x1b[5~",
        "pagedown": b"\x1b[6~",
    }
    if key in special:
        return special[key]
    if char:
        return char.encode("utf-8")
    return b""
