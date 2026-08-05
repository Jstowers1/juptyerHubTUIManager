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
        "ctrl+a": b"\x01",
        "ctrl+b": b"\x02",
        "ctrl+c": b"\x03",
        "ctrl+d": b"\x04",
        "ctrl+f": b"\x06",
        "ctrl+g": b"\x07",
        "ctrl+h": b"\x08",
        "ctrl+i": b"\x09",
        "ctrl+j": b"\x0a",
        "ctrl+k": b"\x0b",
        "ctrl+l": b"\x0c",
        "ctrl+m": b"\x0d",
        "ctrl+n": b"\x0e",
        "ctrl+o": b"\x0f",
        "ctrl+p": b"\x10",
        "ctrl+q": b"\x11",
        "ctrl+r": b"\x12",
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


# Keys that escape the terminal during SSH.
# Each maps to an app action name.
ESCAPE_HATCH_KEYS = {
    "ctrl+t": "toggle_term_focus",
    "ctrl+r": "refresh",
    "ctrl+m": "show_manual",
    "ctrl+k": "setup_keys",
    "ctrl+e": "edit_node",
    "ctrl+h": "show_help",
    "ctrl+g": "git_picker",
    "ctrl+b": "git_branch",
    "ctrl+w": "close_tab",
    "ctrl+o": "activate_venv",
    "ctrl+left": "prev_tab",
    "ctrl+right": "next_tab",
    "ctrl+backslash": "toggle_sidebar",
}


class TerminalDisplay(Widget):

    # Not focusable by default. Only when SSH starts.
    can_focus = False

    DEFAULT_CSS = """
    TerminalDisplay {
        background: #1d1f21;
        color: #c5c8c6;
        padding: 0;
        overflow: hidden;
        width: 1fr;
        height: 1fr;
    }
    """

    class Exited(Message):
        def __init__(self, exit_code: int, terminal_display: "TerminalDisplay") -> None:
            self.exit_code = exit_code
            self.terminal_display = terminal_display
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
        self._command: list[str] = []
        self._pending_start = False
        self._connected = False
        self._overlay_done = False

    @property
    def pty_active(self) -> bool:
        return self._pty_running

    def on_key(self, event) -> None:
        if self._pty_running and self._master_fd is not None:
            action = ESCAPE_HATCH_KEYS.get(event.key)
            if action:
                event.prevent_default()
                event.stop()
                self.app.call_later(self.app.run_action, action)
                return
            self.send_key(event.key, event.character)
            event.prevent_default()
            event.stop()
        elif self._pending_start:
            event.prevent_default()
            event.stop()

    def start(self, command: list[str]) -> None:
        self._command = command
        self._pending_start = True
        self._connected = False
        self._overlay_done = False
        # Focusable immediately so digits/keys go to PTY, not app bindings.
        self.can_focus = True
        self._try_start()
        if self._pending_start:
            self.set_timer(0.05, self._try_start)
            self.set_timer(0.15, self._try_start)
            self.set_timer(0.3, self._try_start)

    def _try_start(self) -> None:
        if not self._pending_start:
            return
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        if w <= 1 or h <= 1:
            return
        self._pending_start = False
        self._pty_running = True
        self.can_focus = True
        self._screen.resize(h, w)
        self._pid, self._master_fd = pty.fork()
        if self._pid == 0:
            # pty.fork already calls setsid and sets controlling tty.
            # Do NOT call setsid again or ssh-add loses /dev/tty.
            winsize = struct.pack("HHHH", h, w, 0, 0)
            try:
                fcntl.ioctl(1, termios.TIOCSWINSZ, winsize)
            except OSError:
                pass
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            os.execvpe(self._command[0], self._command, env)
        else:
            flags = fcntl.fcntl(self._master_fd, fcntl.F_GETFL)
            fcntl.fcntl(self._master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            self._resize_pty()
            self._poll_timer = self.set_interval(0.05, self._poll_pty)

    def send_input(self, data: str) -> None:
        if self._master_fd is not None and self._pty_running:
            try:
                os.write(self._master_fd, data.encode())
            except OSError:
                pass

    def send_key(self, key: str, char: str | None) -> None:
        data = key_to_bytes(key, char)
        if data and self._master_fd is not None and self._pty_running:
            try:
                os.write(self._master_fd, data)
            except OSError:
                pass

    def _resize_pty(self) -> None:
        # Resize PTY and pyte screen to match widget size.
        if self._master_fd is None:
            return
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        try:
            winsize = struct.pack("HHHH", h, w, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass
        self._screen.resize(h, w)
        self._refresh_display()

    def on_resize(self, event) -> None:
        if self._pending_start:
            self._try_start()
        else:
            self._resize_pty()

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
            self._connected = True
            if not self._overlay_done:
                self._overlay_done = True
                self._refresh_display()
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
        self.can_focus = False
        self._stop_timer()
        if self._pid is not None:
            try:
                os.waitpid(self._pid, 0)
            except ChildProcessError:
                pass
            self._pid = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        self._pid = None
        self.post_message(self.Exited(status, self))

    def _refresh_display(self) -> None:
        self._lines = [line.rstrip() for line in self._screen.display]
        self.refresh()

    def render(self) -> Text:
        if not self._overlay_done:
            return Text("Connecting...", style="yellow on #1d1f21")
        if not self._lines:
            return Text(" ", style="white on #1d1f21")
        parts = []
        cursor = self._screen.cursor
        for row, line in enumerate(self._lines):
            if not line:
                line = " "
            if row == cursor.y and self._pty_running:
                col = min(cursor.x, len(line))
                before = line[:col]
                at = line[col] if col < len(line) else " "
                after = line[col + 1:] if col + 1 <= len(line) else ""
                parts.append(Text(before, style="white on #1d1f21"))
                parts.append(Text(at, style="black on white"))
                parts.append(Text(after + "\n", style="white on #1d1f21"))
            else:
                parts.append(Text(line + "\n", style="white on #1d1f21"))
        return Text("").join(parts)

    def stop(self) -> None:
        self._pty_running = False
        self._pending_start = False
        self._connected = False
        self._overlay_done = False
        self.can_focus = False
        self._stop_timer()
        if self._pid is not None:
            try:
                os.killpg(os.getpgid(self._pid), 9)
            except (ProcessLookupError, PermissionError):
                pass
            self._pid = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

    def reset(self) -> None:
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        self._screen = pyte.Screen(w, h)
        self._stream = pyte.Stream(self._screen)
        self._lines = []
        self.refresh()

    def on_unmount(self) -> None:
        self.stop()
