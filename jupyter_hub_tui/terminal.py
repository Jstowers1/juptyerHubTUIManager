# Embedded terminal widget for Textual.
# Runs SSH (or any command) in a PTY, renders output via pyte.

from __future__ import annotations

import os
import pty
import select
import struct
import fcntl
import termios
from typing import Optional

import pyte
from rich.text import Text
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget


def key_to_bytes(key: str, char: str | None) -> bytes:
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
        "ctrl+c": b"\x03",
        "ctrl+d": b"\x04",
        "ctrl+z": b"\x1a",
        "ctrl+l": b"\x0c",
        "ctrl+a": b"\x01",
        "ctrl+e": b"\x05",
        "ctrl+w": b"\x17",
        "ctrl+u": b"\x15",
    }
    if key in special:
        return special[key]
    if char:
        return char.encode("utf-8")
    return b""


class TerminalDisplay(Widget):
    # Renders a pyte screen as Textual content.

    can_focus = True

    DEFAULT_CSS = """
    TerminalDisplay {
        background: #1d1f21;
        color: #c5c8c6;
        padding: 0 1;
        overflow: hidden;
        border: tall $accent;
    }
    """

    class Exited(Message):
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
        self._poll_timer = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, command: list[str]) -> None:
        # Fork a PTY and run the command.
        self._running = True
        self._pid, self._master_fd = pty.fork()
        if self._pid == 0:
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            os.execvpe(command[0], command, env)
        else:
            flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self._poll_timer = self.set_interval(0.05, self._poll_pty)

    def send_key(self, key: str, char: str | None) -> None:
        # Forward a key press to the PTY.
        data = key_to_bytes(key, char)
        if data and self._master_fd is not None and self._running:
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass

    def _stop_timer(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None

    def _poll_pty(self) -> None:
        # Read PTY output, feed to pyte, check exit.
        if not self._running or self._master_fd is None:
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
        if self._pid is not None:
            try:
                pid, status = os.waitpid(self._pid, os.WNOHANG)
                if pid != 0:
                    self._handle_exit(status)
            except ChildProcessError:
                self._handle_exit()

    def _handle_exit(self, status: int = 0) -> None:
        # Clean up PTY state. Called once on exit.
        if not self._running:
            return
        self._running = False
        self._stop_timer()
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        self._pid = None
        self.post_message(self.Exited(status))

    def _refresh_display(self) -> None:
        lines = [line.rstrip() for line in self._screen.display]
        self._lines = lines
        self.refresh()

    def render(self) -> Text:
        if not self._lines:
            return Text("")
        return Text("\n".join(self._lines), style="white on #1d1f21")

    def stop(self) -> None:
        # Kill the PTY process and clean up.
        self._running = False
        self._stop_timer()
        if self._pid is not None:
            try:
                os.kill(self._pid, 15)
            except ProcessLookupError:
                pass
            self._pid = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def on_unmount(self) -> None:
        self.stop()

    def resize(self, cols: int, rows: int) -> None:
        # Resize the pyte screen and PTY.
        self._screen.resize(rows, cols)
        if self._master_fd is not None:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
