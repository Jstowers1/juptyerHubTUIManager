#Embedded terminal widget. Custom screen and ANSI parser, no pyte.
#Run SSH or any command in a PTY and render it with a VT100 state machine.

from __future__ import annotations

import errno
import os
import pty
import select
import struct
import fcntl
import termios
import threading
import queue as _queue
from typing import Optional

from rich.text import Text
from rich.style import Style
from textual.message import Message
from textual.widget import Widget

from .apc import APCStream


def _dbg(msg: str) -> None:
    #Env-gated key-path tracer. Set JHTUI_DEBUG_KEYS=1 to enable it.
    #It only observes and never changes behavior.
    import os as _os
    import time as _time
    if _os.environ.get("JHTUI_DEBUG_KEYS"):
        try:
            with open("/tmp/jhtui-keys.log", "a") as f:
                f.write(f"{_time.time():.6f} {msg}\n")
        except OSError:
            pass


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
    }
    ctrl_map = {
        "a": b"\x01", "b": b"\x02", "c": b"\x03", "d": b"\x04",
        "e": b"\x05", "f": b"\x06", "g": b"\x07", "h": b"\x08",
        "i": b"\x09", "j": b"\x0a", "k": b"\x0b", "l": b"\x0c",
        "m": b"\x0d", "n": b"\x0e", "o": b"\x0f", "p": b"\x10",
        "q": b"\x11", "r": b"\x12", "s": b"\x13", "t": b"\x14",
        "u": b"\x15", "v": b"\x16", "w": b"\x17", "x": b"\x18",
        "y": b"\x19", "z": b"\x1a",
    }
    if key in special:
        return special[key]
    if key.startswith("ctrl+"):
        c = key[5:]
        if len(c) == 1:
            return ctrl_map.get(c.lower(), b"")
    if key.startswith("alt+"):
        ch = key[4:]
        if len(ch) == 1:
            return b"\x1b" + ch.encode()
    if char:
        return char.encode("utf-8")
    return b""


#Keys that escape the terminal during SSH.
#Ctrl+t and arrows are priority app bindings, not listed here.
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

# 16-color palette index to Rich color name.
_PALETTE = [
    "black", "red", "green", "yellow", "blue",
    "magenta", "cyan", "white",
]
_BRIGHT = [
    "grey50", "bright_red", "bright_green", "bright_yellow",
    "bright_blue", "bright_magenta", "bright_cyan", "bright_white",
]


class Cell:
    """One grid cell. Mutable for in-place updates."""
    __slots__ = ("char", "fg", "bg", "bold", "italic", "underline", "strike", "reverse")

    def __init__(self) -> None:
        self.char: str = " "
        self.fg: int = 0  # 0=default, 1-8 palette, 9-16 bright, 17+=256/truecolor
        self.bg: int = 0
        self.bold: bool = False
        self.italic: bool = False
        self.underline: bool = False
        self.strike: bool = False
        self.reverse: bool = False

    def copy_from(self, other: "Cell") -> None:
        self.char = other.char
        self.fg = other.fg
        self.bg = other.bg
        self.bold = other.bold
        self.italic = other.italic
        self.underline = other.underline
        self.strike = other.strike
        self.reverse = other.reverse

    def reset(self) -> None:
        self.char = " "
        self.fg = 0
        self.bg = 0
        self.bold = False
        self.italic = False
        self.underline = False
        self.strike = False
        self.reverse = False

    def is_default(self) -> bool:
        return (
            self.char == " " and self.fg == 0 and self.bg == 0
            and not self.bold and not self.italic and not self.underline
            and not self.strike and not self.reverse
        )


class Screen:
    """Minimal VT100/VT220 screen. Tracks dirty rows."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.cols = cols
        self.rows = rows
        #Flat list of Cell objects in row-major order.
        self.grid: list[list[Cell]] = [
            [Cell() for _ in range(cols)] for _ in range(rows)
        ]
        self.cx = 0
        self.cy = 0
        self.dirty: set[int] = set()
        self._scroll_top = 0
        self._scroll_bot = rows - 1

        #SGR state.
        self.cur_fg = 0
        self.cur_bg = 0
        self.cur_bold = False
        self.cur_italic = False
        self.cur_underline = False
        self.cur_strike = False
        self.cur_reverse = False

        #Saved cursor for DECSC and DECRC.
        self._saved_cx = 0
        self._saved_cy = 0

        #DECTCEM cursor visibility. Apps hide the cursor when they want.
        self.cursor_visible = True

        self._mark_all()

    def resize(self, rows: int, cols: int) -> None:
        new_grid: list[list[Cell]] = []
        for r in range(rows):
            if r < len(self.grid):
                old_row = self.grid[r]
                new_row = old_row[:cols]
                if len(new_row) < cols:
                    new_row += [Cell() for _ in range(cols - len(new_row))]
                new_grid.append(new_row)
            else:
                new_grid.append([Cell() for _ in range(cols)])
        self.grid = new_grid
        self.rows = rows
        self.cols = cols
        self._scroll_top = 0
        self._scroll_bot = rows - 1
        self.cx = min(self.cx, cols - 1)
        self.cy = min(self.cy, rows - 1)
        self._mark_all()

    def _mark_all(self) -> None:
        self.dirty = set(range(self.rows))

    def _mark(self, row: int) -> None:
        self.dirty.add(row)

    def _clamp_cursor(self) -> None:
        self.cx = max(0, min(self.cx, self.cols - 1))
        self.cy = max(0, min(self.cy, self.rows - 1))

    def _current_cell_style(self, cell: Cell) -> None:
        cell.fg = self.cur_fg
        cell.bg = self.cur_bg
        cell.bold = self.cur_bold
        cell.italic = self.cur_italic
        cell.underline = self.cur_underline
        cell.strike = self.cur_strike
        cell.reverse = self.cur_reverse

    def _put_char(self, ch: str) -> None:
        if self.cy >= self.rows:
            return
        row = self.grid[self.cy]
        if self.cx >= self.cols:
            #Auto-wrap.
            self.cx = 0
            self.cy += 1
            if self.cy > self._scroll_bot:
                self._scroll_up(1)
                self.cy = self._scroll_bot
            if self.cy >= self.rows:
                return
            row = self.grid[self.cy]
        cell = row[self.cx]
        cell.char = ch
        self._current_cell_style(cell)
        self._mark(self.cy)
        self.cx += 1

    def _scroll_up(self, n: int = 1) -> None:
        top = self._scroll_top
        bot = self._scroll_bot
        for _ in range(n):
            self.grid.pop(top)
            self.grid.insert(bot, [Cell() for _ in range(self.cols)])
        for r in range(top, bot + 1):
            self._mark(r)

    def _scroll_down(self, n: int = 1) -> None:
        top = self._scroll_top
        bot = self._scroll_bot
        for _ in range(n):
            self.grid.pop(bot)
            self.grid.insert(top, [Cell() for _ in range(self.cols)])
        for r in range(top, bot + 1):
            self._mark(r)

    def _newline(self) -> None:
        self.cy += 1
        if self.cy > self._scroll_bot:
            self._scroll_up(1)
            self.cy = self._scroll_bot

    def _cr(self) -> None:
        self.cx = 0

    def _tab(self) -> None:
        self.cx = min(((self.cx // 8) + 1) * 8, self.cols - 1)

    def _erase_line(self, mode: int = 0) -> None:
        row = self.grid[self.cy]
        if mode == 0:
            for x in range(self.cx, self.cols):
                row[x].reset()
        elif mode == 1:
            for x in range(0, min(self.cx + 1, self.cols)):
                row[x].reset()
        elif mode == 2:
            for x in range(self.cols):
                row[x].reset()
        self._mark(self.cy)

    def _erase_display(self, mode: int = 0) -> None:
        if mode == 0:
            for x in range(self.cx, self.cols):
                self.grid[self.cy][x].reset()
            for r in range(self.cy + 1, self.rows):
                for x in range(self.cols):
                    self.grid[r][x].reset()
                self._mark(r)
            self._mark(self.cy)
        elif mode == 1:
            for r in range(0, self.cy):
                for x in range(self.cols):
                    self.grid[r][x].reset()
                self._mark(r)
            for x in range(0, min(self.cx + 1, self.cols)):
                self.grid[self.cy][x].reset()
            self._mark(self.cy)
        elif mode == 2:
            for r in range(self.rows):
                for x in range(self.cols):
                    self.grid[r][x].reset()
                self._mark(r)

    def _cursor_up(self, n: int = 1) -> None:
        old = self.cy
        self.cy = max(self._scroll_top, self.cy - n)
        if self.cursor_visible:
            self._mark(old)
            self._mark(self.cy)

    def _cursor_down(self, n: int = 1) -> None:
        old = self.cy
        self.cy = min(self._scroll_bot, self.cy + n)
        if self.cursor_visible:
            self._mark(old)
            self._mark(self.cy)

    def _cursor_forward(self, n: int = 1) -> None:
        self.cx = min(self.cols - 1, self.cx + n)
        if self.cursor_visible:
            self._mark(self.cy)

    def _cursor_back(self, n: int = 1) -> None:
        self.cx = max(0, self.cx - n)
        if self.cursor_visible:
            self._mark(self.cy)

    def _set_cursor(self, row: int, col: int) -> None:
        old_r = self.cy
        self.cy = max(0, min(row - 1, self.rows - 1))
        self.cx = max(0, min(col - 1, self.cols - 1))
        if self.cursor_visible:
            self._mark(old_r)
            self._mark(self.cy)

    def _save_cursor(self) -> None:
        self._saved_cx = self.cx
        self._saved_cy = self.cy

    def _restore_cursor(self) -> None:
        self.cx = self._saved_cx
        self.cy = self._saved_cy

    def _set_scroll(self, top: int, bot: int) -> None:
        self._scroll_top = max(0, top - 1)
        self._scroll_bot = min(self.rows - 1, bot - 1)

    def _reset_sgr(self) -> None:
        self.cur_fg = 0
        self.cur_bg = 0
        self.cur_bold = False
        self.cur_italic = False
        self.cur_underline = False
        self.cur_strike = False
        self.cur_reverse = False

    def _apply_sgr(self, params: list[int]) -> None:
        if not params:
            params = [0]
        i = 0
        while i < len(params):
            p = params[i]
            if p == 0:
                self._reset_sgr()
            elif p == 1:
                self.cur_bold = True
            elif p == 3:
                self.cur_italic = True
            elif p == 4:
                self.cur_underline = True
            elif p == 7:
                self.cur_reverse = True
            elif p == 9:
                self.cur_strike = True
            elif p == 22:
                self.cur_bold = False
            elif p == 23:
                self.cur_italic = False
            elif p == 24:
                self.cur_underline = False
            elif p == 27:
                self.cur_reverse = False
            elif p == 29:
                self.cur_strike = False
            elif 30 <= p <= 37:
                self.cur_fg = p - 30 + 1
            elif p == 38:
                #Extended fg. The next param is 5 for 256 colors or 2 for RGB.
                if i + 1 < len(params):
                    mode = params[i + 1]
                    if mode == 5 and i + 2 < len(params):
                        self.cur_fg = 1000 + params[i + 2]
                        i += 2
                    elif mode == 2 and i + 4 < len(params):
                        self.cur_fg = -(params[i + 2] * 65536 + params[i + 3] * 256 + params[i + 4])
                        i += 4
            elif p == 39:
                self.cur_fg = 0
            elif 40 <= p <= 47:
                self.cur_bg = p - 40 + 1
            elif p == 48:
                if i + 1 < len(params):
                    mode = params[i + 1]
                    if mode == 5 and i + 2 < len(params):
                        self.cur_bg = 1000 + params[i + 2]
                        i += 2
                    elif mode == 2 and i + 4 < len(params):
                        self.cur_bg = -(params[i + 2] * 65536 + params[i + 3] * 256 + params[i + 4])
                        i += 4
            elif p == 49:
                self.cur_bg = 0
            elif 90 <= p <= 97:
                self.cur_fg = p - 90 + 10
            elif 100 <= p <= 107:
                self.cur_bg = p - 100 + 10
            i += 1

    #VT100 line drawing charset for G0.
    _DEC_SPECIAL = {
        "`": "\u25c6", "a": "\u2592", "b": "\u2409", "c": "\u240c",
        "d": "\u240d", "e": "\u240a", "f": "\u00b0", "g": "\u00b1",
        "h": "\u2424", "i": "\u240b", "j": "\u2518", "k": "\u2510",
        "l": "\u250c", "m": "\u2514", "n": "\u253c", "o": "\u23ba",
        "p": "\u23bb", "q": "\u2500", "r": "\u23bc", "s": "\u23bd",
        "t": "\u251c", "u": "\u2524", "v": "\u2534", "w": "\u252c",
        "x": "\u2502", "y": "\u2264", "z": "\u2265", "{": "\u03c0",
        "|": "\u2260", "}": "\u00a3", "~": "\u00b7",
    }

    def feed(self, data: str) -> None:
        """Parse ANSI escape sequences and update the grid."""
        i = 0
        n = len(data)
        self.dirty.clear()
        while i < n:
            ch = data[i]
            if ch == "\x1b":
                #Escape sequence.
                if i + 1 >= n:
                    break
                nxt = data[i + 1]
                if nxt == "[":
                    #CSI sequence. Parse the params and the final byte.
                    j = i + 2
                    params_str = ""
                    while j < n and (data[j].isdigit() or data[j] in ";?"):
                        params_str += data[j]
                        j += 1
                    if j < n:
                        final = data[j]
                        #Strip the private-mode marker so int() cannot crash.
                        clean_ps = params_str.lstrip("?")
                        params = [int(p) if p else 0 for p in clean_ps.split(";") if p != ""]
                        self._handle_csi(final, params, clean_ps)
                        i = j + 1
                    else:
                        break
                elif nxt == "]":
                    #OSC sequence. Skip until BEL or ST.
                    j = i + 2
                    while j < n and data[j] != "\x07" and data[j: j + 2] != "\x1b\\":
                        j += 1
                    if j < n:
                        i = j + (2 if data[j: j + 2] == "\x1b\\" else 1)
                    else:
                        break
                elif nxt == "(":
                    #G0 charset designate.
                    if i + 2 < n:
                        charset = data[i + 2]
                        #We track this per-feed since it's rare.
                        #DEC Special Graphics = '0'. Set a flag and translate.
                        if charset == "0":
                            #Translate chars until the next charset switch.
                            j = i + 3
                            while j < n:
                                if data[j] == "\x1b":
                                    break
                                mapped = self._DEC_SPECIAL.get(data[j])
                                if mapped:
                                    self._put_char(mapped)
                                else:
                                    self._put_char(data[j])
                                j += 1
                            i = j
                        else:
                            i += 3
                    else:
                        break
                elif nxt == ")":
                    FIXUP_G1
                    i += 3
                elif nxt == "D":
                    self._newline()
                    i += 2
                elif nxt == "M":
                    if self.cy == self._scroll_top:
                        self._scroll_down(1)
                    else:
                        self.cy -= 1
                    i += 2
                elif nxt == "E":
                    self._newline()
                    self._cr()
                    i += 2
                elif nxt == "7":
                    self._save_cursor()
                    i += 2
                elif nxt == "8":
                    self._restore_cursor()
                    i += 2
                elif nxt == "=":
                    #Application keypad mode.
                    i += 2
                elif nxt == ">":
                    #Normal keypad mode.
                    i += 2
                elif nxt == "c":
                    #RIS reset.
                    self.__init__(self.cols, self.rows)
                    i += 2
                else:
                    i += 2
            elif ch == "\r":
                self._cr()
                i += 1
            elif ch == "\n":
                self._newline()
                i += 1
            elif ch == "\t":
                self._tab()
                i += 1
            elif ch == "\x08":
                self._cursor_back(1)
                i += 1
            elif ch == "\x07":
                #Bell. Ignore.
                i += 1
            elif ch == "\x0f":
                #SI shift in. Ignore.
                i += 1
            elif ch == "\x0e":
                #SO shift out. Ignore.
                i += 1
            elif ord(ch) < 32:
                #Other control chars. Skip.
                i += 1
            else:
                #Printable char.
                self._put_char(ch)
                i += 1

    def _handle_csi(self, final: str, params: list[int], raw: str) -> None:
        if final == "A":
            self._cursor_up(params[0] if params and params[0] else 1)
        elif final == "B":
            self._cursor_down(params[0] if params and params[0] else 1)
        elif final == "C":
            self._cursor_forward(params[0] if params and params[0] else 1)
        elif final == "D":
            self._cursor_back(params[0] if params and params[0] else 1)
        elif final == "E":
            n = params[0] if params and params[0] else 1
            self.cy = min(self._scroll_bot, self.cy + n)
            self.cx = 0
        elif final == "F":
            n = params[0] if params and params[0] else 1
            self.cy = max(self._scroll_top, self.cy - n)
            self.cx = 0
        elif final == "G":
            col = params[0] if params else 1
            self.cx = max(0, min(col - 1, self.cols - 1))
        elif final == "H" or final == "f":
            row = params[0] if len(params) > 0 and params[0] else 1
            col = params[1] if len(params) > 1 and params[1] else 1
            self._set_cursor(row, col)
        elif final == "J":
            self._erase_display(params[0] if params else 0)
        elif final == "K":
            self._erase_line(params[0] if params else 0)
        elif final == "m":
            self._apply_sgr(params)
        elif final == "r":
            top = params[0] if len(params) > 0 and params[0] else 1
            bot = params[1] if len(params) > 1 and params[1] else self.rows
            self._set_scroll(top, bot)
        elif final == "d":
            row = params[0] if params else 1
            self.cy = max(0, min(row - 1, self.rows - 1))
        elif final == "L":
            n = params[0] if params and params[0] else 1
            for _ in range(n):
                self.grid.pop(self._scroll_bot)
                self.grid.insert(self.cy, [Cell() for _ in range(self.cols)])
            for r in range(self.cy, self._scroll_bot + 1):
                self._mark(r)
        elif final == "M":
            n = params[0] if params and params[0] else 1
            for _ in range(n):
                self.grid.pop(self._scroll_bot)
                self.grid.insert(self.cy, [Cell() for _ in range(self.cols)])
            for r in range(self.cy, self._scroll_bot + 1):
                self._mark(r)
        elif final == "P":
            n = params[0] if params and params[0] else 1
            row = self.grid[self.cy]
            for _ in range(n):
                if self.cx < len(row):
                    row.pop(self.cx)
                    row.append(Cell())
            self._mark(self.cy)
        elif final == "@":
            n = params[0] if params and params[0] else 1
            row = self.grid[self.cy]
            for _ in range(n):
                row.insert(self.cx, Cell())
                row.pop()
            self._mark(self.cy)
        elif final == "S":
            self._scroll_up(params[0] if params and params[0] else 1)
        elif final == "T":
            self._scroll_down(params[0] if params and params[0] else 1)
        elif final == "h" or final == "l":
            #Mode set or reset. 25 = DECTCEM cursor visibility.
            on = final == "h"
            for p in params:
                if p == 25:
                    if self.cursor_visible != on:
                        self.cursor_visible = on
                        self._mark(self.cy)
        elif final == "n":
            pass  #Device status report. Ignore.
        elif final == "c":
            pass  #Device attributes. Ignore.
        elif final == "s":
            self._save_cursor()
        elif final == "u":
            self._restore_cursor()
        #Unknown CSI. Ignore.

    def _color_to_rich(self, val: int) -> str:
        if val == 0:
            return ""
        if 1 <= val <= 8:
            return _PALETTE[val - 1]
        if 9 <= val <= 16:
            return _BRIGHT[val - 9]
        if val >= 1000:
            idx = val - 1000
            if idx < 8:
                return _PALETTE[idx]
            elif idx < 16:
                return _BRIGHT[idx - 8]
            elif idx < 232:
                idx -= 16
                r = idx // 36
                g = (idx % 36) // 6
                b = idx % 6
                if r == g == b:
                    v = 8 + r * 10
                    return f"#{v:02x}{v:02x}{v:02x}"
                return f"#{r * 51:02x}{g * 51:02x}{b * 51:02x}"
            else:
                v = 8 + (idx - 232) * 10
                return f"#{v:02x}{v:02x}{v:02x}"
        if val < 0:
            rgb = -val
            r = rgb >> 16
            g = (rgb >> 8) & 0xFF
            b = rgb & 0xFF
            return f"#{r:02x}{g:02x}{b:02x}"
        return ""

    def render_row(self, y: int) -> Text:
        row = self.grid[y]
        #Cursor cell renders reverse video. Draws the block cursor.
        cur = self.cursor_visible and y == self.cy
        parts: list[Text] = []
        run_text = ""
        run_style = ""

        def flush() -> None:
            nonlocal run_text, run_style
            if run_text:
                parts.append(Text(run_text, style=run_style or ""))
                run_text = ""
                run_style = ""

        for x in range(self.cols):
            cell = row[x]
            if cell.reverse:
                fg = cell.bg
                bg = cell.fg
            else:
                fg = cell.fg
                bg = cell.bg

            style_parts: list[str] = []
            if cur and x == self.cx:
                style_parts.append("reverse")
            if fg:
                c = self._color_to_rich(fg)
                if c:
                    style_parts.append(c)
            if bg:
                c = self._color_to_rich(bg)
                if c:
                    style_parts.append("on " + c)
            if cell.bold:
                style_parts.append("bold")
            if cell.italic:
                style_parts.append("italic")
            if cell.underline:
                style_parts.append("underline")
            if cell.strike:
                style_parts.append("strike")

            style = " ".join(style_parts)
            char = cell.char if cell.char else " "

            if style != run_style:
                flush()
            run_text += char
            run_style = style

        flush()
        if not parts:
            return Text(" " * self.cols)
        return Text("").join(parts)


class TerminalDisplay(Widget):

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
        self._screen = Screen(80, 24)
        self._master_fd: Optional[int] = None
        self._pid: Optional[int] = None
        self._pty_running = False
        self._poll_timer: Optional[object] = None
        self._command: list[str] = []
        self._pending_start = False
        self._connected = False
        self._overlay_done = False
        self._apc = APCStream()
        #Row render cache. Rebuild only dirty rows.
        self._row_cache: list[Text] = [Text(" " * 80) for _ in range(24)]
        self._cached_render: Optional[Text] = None
        self._pending_text: str = ""
        self._read_queue: _queue.Queue = _queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self._grid_lock = threading.Lock()

    @property
    def pty_active(self) -> bool:
        return self._pty_running

    def on_key(self, event) -> None:
        if self._pty_running and self._master_fd is not None:
            if event.key in ESCAPE_HATCH_KEYS:
                _dbg(f"on_key ESCAPE key={event.key!r}")
                event.stop()
                self.app.call_later(self.app.run_action, ESCAPE_HATCH_KEYS[event.key])
                return
            _dbg(f"on_key key={event.key!r} char={event.character!r} focused={self.has_focus}")
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
            #Start the reader thread. Never block the loop on PTY I/O.
            self._reader_stop.clear()
            self._reader_thread = threading.Thread(
                target=self._reader_loop, daemon=True
            )
            self._reader_thread.start()
            self._poll_timer = self.set_interval(0.016, self._drain_queue)

    def _reader_loop(self) -> None:
        #Background thread reads the PTY, parses ANSI and renders rows.
        #The loop only joins ready Text objects. No parsing on the main thread.
        fd = self._master_fd
        screen = self._screen
        while not self._reader_stop.is_set():
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not ready:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                self._read_queue.put(None)
                return
            if not data:
                self._read_queue.put(None)
                return
            text = data.decode("utf-8", errors="replace")
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
            if not clean:
                continue
            dirty_rows = {}
            with self._grid_lock:
                screen.feed(clean)
                for y in screen.dirty:
                    if y < len(self._row_cache):
                        dirty_rows[y] = screen.render_row(y)
                screen.dirty.clear()
            if dirty_rows:
                #Cap backlog. Drop oldest when paused so resume does not flood.
                if self._read_queue.qsize() > 8:
                    try:
                        self._read_queue.get_nowait()
                    except _queue.Empty:
                        pass
                self._read_queue.put(dirty_rows)

    def send_input(self, data: str) -> None:
        if self._master_fd is not None and self._pty_running:
            try:
                os.write(self._master_fd, data.encode())
            except OSError:
                pass

    def send_key(self, key: str, char: str | None) -> None:
        #Only os.write and return. No refresh here.
        data = key_to_bytes(key, char)
        if not data or self._master_fd is None or not self._pty_running:
            _dbg(f"send_key DROP key={key!r} data={data!r} running={self._pty_running}")
            return
        fd = self._master_fd
        for attempt in range(5):
            try:
                os.write(fd, data)
                _dbg(f"send_key WROTE key={key!r} bytes={data!r} attempt={attempt}")
                return
            except OSError as e:
                if e.errno != errno.EAGAIN:
                    _dbg(f"send_key ERR key={key!r} errno={e.errno}")
                    return
                _dbg(f"send_key EAGAIN key={key!r} attempt={attempt}")
                #Buffer full. Wait for write-readiness, not a spin.
                try:
                    select.select([], [fd], [], 0.01)
                except (OSError, ValueError):
                    return

    def _drain_queue(self) -> None:
        if not self._pty_running:
            return
        dirty = False
        while True:
            try:
                item = self._read_queue.get_nowait()
            except _queue.Empty:
                break
            if item is None:
                self._handle_exit()
                return
            self._connected = True
            if not self._overlay_done:
                self._overlay_done = True
            for y, row_text in item.items():
                if y < len(self._row_cache):
                    self._row_cache[y] = row_text
            self._cached_render = None
            dirty = True
        if dirty:
            self.refresh()

    def _resize_pty(self) -> None:
        if self._master_fd is None:
            return
        w = max(1, self.size.width)
        h = max(1, self.size.height)
        try:
            winsize = struct.pack("HHHH", h, w, 0, 0)
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass
        with self._grid_lock:
            self._screen.resize(h, w)
            self._row_cache = [self._screen.render_row(y) for y in range(self._screen.rows)]
        self._cached_render = None
        self.refresh()

    def on_resize(self, event) -> None:
        if self._pending_start:
            self._try_start()
        else:
            self._resize_pty()

    #Timer drains the queue at 60fps. Pause and resume control visibility.
    def pause_polling(self) -> None:
        #Drain stale items so resume does not replay an old backlog.
        _dbg("pause_polling")
        while True:
            try:
                self._read_queue.get_nowait()
            except _queue.Empty:
                break
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None

    def resume_polling(self) -> None:
        _dbg("resume_polling")
        if self._poll_timer is None and self._pty_running:
            self._poll_timer = self.set_interval(0.016, self._drain_queue)

    def _handle_exit(self, status: int = 0) -> None:
        if not self._pty_running:
            return
        self._pty_running = False
        self.can_focus = False
        self.pause_polling()
        self._reader_stop.set()
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

    def render(self) -> Text:
        if not self._overlay_done:
            return Text("Connecting...", style="yellow on #1d1f21")
        if self._cached_render is None:
            self._cached_render = Text("\n").join(self._row_cache)
        return self._cached_render

    def stop(self) -> None:
        self._pty_running = False
        self._pending_start = False
        self._connected = False
        self._overlay_done = False
        self.can_focus = False
        self.pause_polling()
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
        with self._grid_lock:
            self._screen = Screen(w, h)
            self._row_cache = [self._screen.render_row(y) for y in range(self._screen.rows)]
        self._cached_render = None
        self.refresh()

    def on_unmount(self) -> None:
        self.stop()


if __name__ == "__main__":
    #Self-check text rendering.
    s = Screen(20, 5)
    s.feed("Hello")
    assert s.grid[0][0].char == "H", f"got {s.grid[0][0].char}"
    assert s.grid[0][4].char == "o", f"got {s.grid[0][4].char}"
    assert s.cx == 5, f"cx={s.cx}"
    print("test1 pass: basic text")

    #Self-check newline and carriage return.
    s = Screen(20, 5)
    s.feed("AB\r\nCD")
    assert s.grid[0][0].char == "A"
    assert s.grid[1][0].char == "C", f"got {s.grid[1][0].char}"
    print("test2 pass: newline")

    #Self-check cursor movement.
    s = Screen(20, 5)
    s.feed("\x1b[3;5HX")
    assert s.grid[2][4].char == "X", f"got {s.grid[2][4].char}"
    print("test3 pass: cursor positioning")

    #Self-check SGR colors.
    s = Screen(20, 5)
    s.feed("\x1b[31mR\x1b[0mN")
    assert s.grid[0][0].char == "R"
    assert s.grid[0][0].fg == 2, f"fg={s.grid[0][0].fg}"
    assert s._color_to_rich(2) == "red"
    assert s.grid[0][1].char == "N"
    assert s.grid[0][1].fg == 0
    print("test4 pass: SGR colors")

    #Self-check erase line.
    s = Screen(10, 5)
    s.feed("ABCDEFGH\x1b[2K")
    assert s.grid[0][0].char == " ", f"got {s.grid[0][0].char}"
    print("test5 pass: erase line")

    #Self-check: scroll up.
    s = Screen(10, 3)
    s.feed("L0\r\nL1\r\nL2\r\nL3")
    assert s.grid[0][0].char == "L"
    assert s.grid[0][1].char == "1", f"row0={s.grid[0][1].char}"
    print("test6 pass: scroll")

    #Self-check dirty tracking.
    s = Screen(10, 3)
    s.feed("Hi")
    assert 0 in s.dirty, f"dirty={s.dirty}"
    assert 1 not in s.dirty
    print("test7 pass: dirty tracking")

    #Self-check insert and delete char.
    s = Screen(10, 1)
    s.feed("ABCDE\x1b[1G\x1b[P")
    assert s.grid[0][0].char == "B", f"got {s.grid[0][0].char}"
    print("test8 pass: delete char")

    #Self-check render row returns Text.
    s = Screen(10, 1)
    s.feed("Test")
    t = s.render_row(0)
    assert "Test" in t.plain, f"got {t.plain!r}"
    print("test9 pass: render row")

    #Self-check bold and underline.
    s = Screen(10, 1)
    s.feed("\x1b[1;4mB\x1b[0m")
    assert s.grid[0][0].bold, "bold not set"
    assert s.grid[0][0].underline, "underline not set"
    print("test10 pass: bold+underline")

    #Self-check reverse video.
    s = Screen(10, 1)
    s.feed("\x1b[7mR\x1b[0m")
    assert s.grid[0][0].reverse, "reverse not set"
    print("test11 pass: reverse")

    #Self-check 256 colors.
    s = Screen(10, 1)
    s.feed("\x1b[38;5;196mA")
    assert s.grid[0][0].fg == 1000 + 196, f"fg={s.grid[0][0].fg}"
    print("test12 pass: 256-color")

    #Self-check truecolor.
    s = Screen(10, 1)
    s.feed("\x1b[38;2;128;64;255mA")
    expected = -(128 * 65536 + 64 * 256 + 255)
    assert s.grid[0][0].fg == expected, f"fg={s.grid[0][0].fg}"
    print("test13 pass: truecolor")

    #Self-check DEC special graphics.
    s = Screen(10, 1)
    s.feed("\x1b(0lqk\x1b(B")
    assert s.grid[0][0].char == "\u250c", f"got {s.grid[0][0].char!r}"
    assert s.grid[0][1].char == "\u2500", f"got {s.grid[0][1].char!r}"
    print("test14 pass: DEC special graphics")

    #Self-check private-mode CSI bracketed paste.
    s = Screen(10, 1)
    s.feed("\x1b[?2004h")
    s.feed("\x1b[?2004l")
    print("test15 pass: private-mode CSI")

    #Self-check cursor visibility and block render.
    s = Screen(10, 3)
    s.feed("hi\x1b[3;5HX")
    assert s.cy == 2 and s.cx == 5, f"pos {s.cy},{s.cx}"
    row = s.render_row(2).plain
    assert row[4] == "X", f"char {row[4]!r}"
    s2 = s.render_row(2)
    assert any("reverse" in str(sp.style) for sp in s2.spans), "cursor not reverse"
    s.feed("\x1b[?25l")
    assert not s.cursor_visible, "hide failed"
    s3 = s.render_row(2)
    assert not any("reverse" in str(sp.style) for sp in s3.spans), "still reverse"
    s.feed("\x1b[?25h")
    assert s.cursor_visible, "show failed"
    print("test16 pass: cursor visibility + block render")

    print("ALL PASS")
