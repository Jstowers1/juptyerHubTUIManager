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

from .apc import APCStream


def key_to_bytes(key: str, char: str | None) -> bytes:
    special = {
        "enter": b"\r",
        "shift+enter": b"\x1b\r",
        "ctrl+enter": b"\r",
        "alt+enter": b"\x1b\r",
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
# Ctrl+t/left/right/backslash are priority app bindings, not here.
ESCAPE_HATCH_KEYS = {
    "ctrl+m": "show_manual",
    "ctrl+k": "setup_keys",
    "ctrl+e": "edit_node",
    "ctrl+h": "show_help",
    "ctrl+g": "git_picker",
    "ctrl+b": "git_branch",
    "ctrl+w": "close_tab",
    "ctrl+o": "activate_venv",
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
        self._apc = APCStream()

    @property
    def pty_active(self) -> bool:
        return self._pty_running

    def on_key(self, event) -> None:
        if self._pty_running and self._master_fd is not None:
            hatches = ESCAPE_HATCH_KEYS
            if event.key in hatches:
                event.stop()
                self.app.call_later(self.app.run_action, hatches[event.key])
                return
            self.send_key(event.key, event.character)
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
            self._poll_timer = self.set_interval(0.1, self._poll_pty)

    def send_input(self, data: str) -> None:
        if self._master_fd is not None and self._pty_running:
            try:
                os.write(self._master_fd, data.encode())
            except OSError:
                pass

    def send_key(self, key: str, char: str | None) -> None:
        import errno
        data = key_to_bytes(key, char)
        if not data or self._master_fd is None or not self._pty_running:
            return
        for _ in range(10):
            try:
                os.write(self._master_fd, data)
                return
            except OSError as e:
                if e.errno == errno.EAGAIN:
                    import time
                    time.sleep(0.002)
                    continue
                return

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

    def _process_apc(self, text: str) -> str:
        # Parse APC sequences. Forward them to the real terminal.
        # Return clean text for pyte.
        clean, apcs = self._apc.feed(text)
        if apcs:
            import sys
            stdout = sys.__stdout__
            if stdout is not None:
                try:
                    for seq in apcs:
                        stdout.write(seq)
                    stdout.flush()
                except (OSError, ValueError):
                    pass
        return clean

    def _poll_pty(self) -> None:
        if not self._pty_running or self._master_fd is None:
            return
        chunks = []
        while True:
            try:
                ready, _, _ = select.select([self._master_fd], [], [], 0)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                data = os.read(self._master_fd, 65536)
            except OSError:
                self._handle_exit()
                return
            if not data:
                self._handle_exit()
                return
            chunks.append(data)
        if chunks:
            self._connected = True
            if not self._overlay_done:
                self._overlay_done = True
            text = b"".join(chunks).decode("utf-8", errors="replace")
            text = self._process_apc(text)
            if text:
                self._stream.feed(text)
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
        self.refresh()

    PYTE_TO_RICH = {
        "default": "",
        "brown": "yellow",
        "brightblack": "grey50",
        "brightred": "bright_red",
        "brightgreen": "bright_green",
        "brightbrown": "bright_yellow",
        "brightblue": "bright_blue",
        "brightmagenta": "bright_magenta",
        "brightcyan": "bright_cyan",
        "brightwhite": "bright_white",
    }

    def _pyte_color(self, val: str) -> str:
        mapped = self.PYTE_TO_RICH.get(val, val)
        if mapped and mapped != "default":
            if all(c in "0123456789abcdef" for c in mapped) and len(mapped) == 6:
                return f"#{mapped}"
        return mapped

    def _cell_style(self, cell, row: int, cursor_y: int) -> str:
        if cell.reverse:
            fg, bg = cell.bg, cell.fg
        else:
            fg, bg = cell.fg, cell.bg
        parts = []
        if fg and fg != "default":
            parts.append(self._pyte_color(fg))
        if bg and bg != "default":
            parts.append("on " + self._pyte_color(bg))
        if cell.bold:
            parts.append("bold")
        if cell.italics:
            parts.append("italic")
        if cell.underscore:
            parts.append("underline")
        if cell.strikethrough:
            parts.append("strike")
        return " ".join(parts)

    def _render_row(self, y: int, cursor_y: int) -> Text:
        row_line = self._screen.buffer[y]
        is_cursor_row = (y == cursor_y and self._pty_running)
        cursor_x = self._screen.cursor.x if is_cursor_row else -1
        parts = []
        run_text = ""
        run_style = None
        for x in range(self._screen.columns):
            cell = row_line[x]
            style = self._cell_style(cell, y, cursor_y)
            if x == cursor_x:
                style = (style + " reverse") if style else "reverse"
            char = cell.data if cell.data else " "
            if style != run_style:
                if run_text:
                    parts.append(Text(run_text, style=run_style or ""))
                run_text = char
                run_style = style
            else:
                run_text += char
        if run_text:
            parts.append(Text(run_text, style=run_style or ""))
        if not parts:
            return Text(" ")
        return Text("").join(parts)

    def render(self) -> Text:
        if not self._overlay_done:
            return Text("Connecting...", style="yellow on #1d1f21")
        cursor_y = self._screen.cursor.y
        rows = [self._render_row(y, cursor_y) for y in range(self._screen.lines)]
        return Text("\n").join(rows)

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
        self.refresh()

    def on_unmount(self) -> None:
        self.stop()
