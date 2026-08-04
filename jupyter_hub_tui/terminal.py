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
from textual.binding import Binding


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
        "ctrl+a": b"\x01",
        "ctrl+b": b"\x02",
        "ctrl+c": b"\x03",
        "ctrl+d": b"\x04",
        "ctrl+f": b"\x06",
        "ctrl+g": b"\x07",
        "ctrl+h": b"\x08",
        "ctrl+i": b"\x09",
        "ctrl+k": b"\x0b",
        "ctrl+l": b"\x0c",
        "ctrl+o": b"\x0f",
        "ctrl+p": b"\x10",
        "ctrl+q": b"\x11",
        "ctrl+s": b"\x13",
        "ctrl+t": b"\x14",
        "ctrl+u": b"\x15",
        "ctrl+v": b"\x16",
        "ctrl+w": b"\x17",
        "ctrl+x": b"\x18",
        "ctrl+y": b"\x19",
        "ctrl+z": b"\x1a",
    }
    if key in special:
        return special[key]
    if char:
        return char.encode("utf-8")
    return b""


class TerminalDisplay(Widget):

    can_focus = True

    DEFAULT_CSS = """
    TerminalDisplay {
        background: #1d1f21;
        color: #c5c8c6;
        padding: 0 1;
        overflow: hidden;
    }
    """

    class Exited(Message):
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
        self._pty_running = False
        self._poll_timer = None

    @property
    def pty_active(self) -> bool:
        # Do NOT override Widget.is_running. That breaks Textual's
        # message pump checks. Use a separate name.
        return self._pty_running

    def on_key(self, event) -> None:
        # When PTY is running, eat all keys and send to the process.
        # App priority bindings (ctrl+n etc) already fired before this.
        if self._pty_running and self._master_fd is not None:
            self.send_key(event.key, event.character)
            event.prevent_default()
            event.stop()

    def action_send_tab(self) -> None:
        # Send tab to PTY instead of letting focus_next steal it.
        self.send_key("tab", "\t")

    def start(self, command: list[str]) -> None:
        self._pty_running = True
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
        data = key_to_bytes(key, char)
        if data and self._master_fd is not None and self._pty_running:
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass

    def _stop_timer(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None

    def _poll_pty(self) -> None:
        if not self._pty_running or self._master_fd is None:
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
        if not self._pty_running:
            return
        self._pty_running = False
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
        self._lines = [line.rstrip() for line in self._screen.display]
        self.refresh()

    def render(self) -> Text:
        if not self._lines:
            return Text("")
        return Text("\n".join(self._lines), style="white on #1d1f21")

    def stop(self) -> None:
        self._pty_running = False
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

    def reset(self) -> None:
        self._screen = pyte.Screen(80, 24)
        self._stream = pyte.Stream(self._screen)
        self._lines = []
        self.refresh()

    def on_unmount(self) -> None:
        self.stop()
