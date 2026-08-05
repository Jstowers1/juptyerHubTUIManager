# Notebook viewer widget. Renders .ipynb cells, executes via RemoteKernel,
# displays images via textual-image kitty graphics protocol.

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass, field

import nbformat
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static, TextArea

from .kernel_client import CellResult, RemoteKernel


@dataclass
class CellState:
    source: str
    cell_type: str = "code"
    outputs: list[dict] = field(default_factory=list)
    result: CellResult | None = None
    running: bool = False


class CellCard(Widget):
    # One cell: editor + output area.

    DEFAULT_CSS = """
    CellCard {
        height: auto;
        min-height: 3;
        margin: 0 0 1 0;
        padding: 0;
        border: round $primary;
    }
    CellCard.running {
        border: round $accent;
    }
    CellCard.error {
        border: round $error;
    }
    CellCard TextArea {
        height: auto;
        min-height: 3;
        border: none;
    }
    CellCard .cell-output {
        height: auto;
        min-height: 0;
        padding: 0 1;
        background: $surface;
        display: none;
    }
    CellCard .cell-output.has-content {
        display: block;
    }
    CellCard .cell-images {
        height: auto;
        min-height: 0;
        padding: 0 1;
        display: none;
    }
    CellCard .cell-images.has-images {
        display: block;
    }
    CellCard .cell-type-badge {
        dock: top;
        width: auto;
        height: 1;
        color: $text-muted;
        text-style: bold;
        padding: 0 1;
    }
    """

    def __init__(self, cell: CellState, index: int) -> None:
        super().__init__()
        self.cell = cell
        self.index = index

    def compose(self) -> ComposeResult:
        yield Static(
            f"[{self.index}] {self.cell.cell_type}",
            classes="cell-type-badge",
        )
        editor = TextArea(
            text=self.cell.source,
            classes="cell-editor",
        )
        yield editor
        output = Static("", classes="cell-output")
        yield output
        yield VerticalScroll(classes="cell-images")

    def on_mount(self) -> None:
        self._render_output()

    def get_source(self) -> str:
        try:
            return self.query_one(TextArea).text
        except Exception:
            return self.cell.source

    def set_running(self, running: bool) -> None:
        self.cell.running = running
        self.remove_class("running" if not running else "error")
        if running:
            self.add_class("running")
        self._render_output()

    def set_result(self, result: CellResult) -> None:
        self.cell.result = result
        self.cell.running = False
        self.remove_class("running")
        if result.error:
            self.add_class("error")
        self._render_output()

    def _render_output(self) -> None:
        try:
            out = self.query_one(".cell-output", Static)
        except Exception:
            return
        import re
        parts: list[str] = []
        if self.cell.running:
            parts.append("[dim italic]running...[/]")
        r = self.cell.result
        if r is not None:
            if r.stdout:
                parts.append(r.stdout.rstrip())
            if r.stderr:
                parts.append(f"[red]{r.stderr.rstrip()}[/]")
            if r.error:
                # Strip ANSI color codes that break Textual markup parser.
                clean = re.sub(r"\x1b\[[0-9;]*m", "", r.error)
                parts.append(f"[bold red]{clean}[/]")
            n_imgs = len(r.images)
            if n_imgs:
                parts.append(f"[dim][{n_imgs} image(s) below][/]")
        if parts:
            out.update("\n".join(parts))
            out.add_class("has-content")
        else:
            out.update("")
            out.remove_class("has-content")

    def get_images(self) -> list[bytes]:
        r = self.cell.result
        if r is not None:
            return r.images
        return []

    @property
    def has_images(self) -> bool:
        return len(self.get_images()) > 0


class NotebookView(Widget):
    # Full notebook renderer with kernel-backed execution.

    DEFAULT_CSS = """
    NotebookView {
        height: 1fr;
        width: 1fr;
    }
    NotebookView VerticalScroll {
        height: 1fr;
    }
    #nb-status {
        dock: bottom;
        height: 1;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+e", "run_cell", "Run", show=True, priority=True),
        Binding("ctrl+r", "run_and_next", "Run+Next", show=True, priority=True),
        Binding("ctrl+s", "save", "Save", show=False, priority=True),
        Binding("ctrl+i", "interrupt", "Interrupt", show=False, priority=True),
        Binding("ctrl+k", "prev_cell", "Prev", show=False, priority=True),
        Binding("ctrl+j", "next_cell", "Next", show=False, priority=True),
    ]

    class KernelStarted(Message):
        pass

    def __init__(
        self,
        remote_path: str,
        node_name: str,
        ssh,
        config: dict,
    ) -> None:
        super().__init__()
        self.remote_path = remote_path
        self.node_name = node_name
        self._ssh = ssh
        self._config = config
        self._cells: list[CellState] = []
        self._kernel: RemoteKernel | None = None
        self._active_cell = 0

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="nb-scroll")
        yield Static("Loading notebook...", id="nb-status")

    def on_focus(self) -> None:
        # When NotebookView gets focus (e.g. via Ctrl+T), cascade into
        # the active cell's TextArea.
        self._focus_cell(self._active_cell)

    def on_mount(self) -> None:
        self.run_worker(self._load_and_start, exclusive=True)

    async def _load_and_start(self) -> None:
        from . import config as cfg

        status = self.query_one("#nb-status", Static)
        status.update("[yellow]Loading notebook...[/]")
        # Load notebook from remote.
        loop = asyncio.get_event_loop()
        raw = await loop.run_in_executor(
            None,
            self._ssh.read_remote_file,
            self.node_name,
            self.remote_path,
        )
        if raw is None:
            err = self._ssh._last_error or "unknown"
            status.update(f"[red]Failed to load notebook: {err}[/]")
            return
        nb = nbformat.reads(raw.decode(), as_version=4)
        self._cells = []
        for c in nb.cells:
            if c.cell_type not in ("code", "markdown"):
                continue
            cs = CellState(
                source=c.source.rstrip(),
                cell_type=c.cell_type,
                outputs=getattr(c, "outputs", []),
            )
            self._cells.append(cs)
        status.update(f"[yellow]Starting kernel...[/]")
        # Start remote kernel.
        venv_cmd = cfg.venv_activate_cmd(self._config)
        pythonpath = cfg.venv_pythonpath(self._config)
        self._kernel = RemoteKernel(
            self._ssh, self.node_name, venv_cmd, pythonpath
        )
        try:
            await loop.run_in_executor(None, self._kernel.start)
        except Exception as e:
            status.update(f"[red]Kernel failed: {e}[/]")
            return
        status.update(f"[green]Kernel ready[/]  {len(self._cells)} cells")
        self._render_cells()
        self.post_message(self.KernelStarted())

    def _render_cells(self) -> None:
        scroll = self.query_one("#nb-scroll", VerticalScroll)
        scroll.remove_children()
        for i, cell in enumerate(self._cells):
            card = CellCard(cell, i)
            scroll.mount(card)
        if self._cells:
            self._focus_cell(0)

    def _focus_cell(self, idx: int) -> None:
        if not self._cells:
            return
        idx = max(0, min(idx, len(self._cells) - 1))
        self._active_cell = idx
        scroll = self.query_one("#nb-scroll", VerticalScroll)
        cards = list(scroll.query(CellCard))
        if idx < len(cards):
            try:
                cards[idx].query_one(TextArea).focus()
                scroll.scroll_to_widget(cards[idx])
            except Exception:
                pass

    def _current_card(self) -> CellCard | None:
        # Find which CellCard owns the focused TextArea.
        scroll = self.query_one("#nb-scroll", VerticalScroll)
        cards = list(scroll.query(CellCard))
        if not cards:
            return None
        focused = self.app.focused
        for card in cards:
            if focused is card or (
                focused is not None and card in focused.ancestors
            ):
                self._active_cell = card.index
                return card
        # Fall back to tracked index.
        if self._active_cell < len(cards):
            return cards[self._active_cell]
        return cards[0]

    def action_prev_cell(self) -> None:
        self._focus_cell(self._active_cell - 1)

    def action_next_cell(self) -> None:
        self._focus_cell(self._active_cell + 1)

    def action_run_cell(self) -> None:
        self.run_worker(self._run_cell(run_next=False), exclusive=True)

    def action_run_and_next(self) -> None:
        self.run_worker(self._run_cell(run_next=True), exclusive=True)

    async def _run_cell(self, run_next: bool = False) -> None:
        if self._kernel is None:
            self.notify("Kernel not ready", severity="warning")
            return
        card = self._current_card()
        if card is None:
            return
        code = card.get_source()
        card.cell.source = code
        if card.cell.cell_type != "code":
            status = self.query_one("#nb-status", Static)
            status.update(f"[dim]Cell {card.index} is markdown (skipped)[/]")
            if run_next:
                self._focus_cell(self._active_cell + 1)
            return
        card.set_running(True)
        status = self.query_one("#nb-status", Static)
        status.update(f"[yellow]Running cell {card.index}...[/]")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self._kernel.execute, code
        )
        card.set_result(result)
        await self._refresh_card_images(card)
        if result.error:
            status.update(f"[red]Cell {card.index} error[/]")
        else:
            status.update(f"[green]Cell {card.index} done[/]  {len(result.images)} image(s)")
        if run_next:
            self._focus_cell(self._active_cell + 1)

    async def _refresh_card_images(self, card: CellCard) -> None:
        images = card.get_images()
        if not images:
            return
        try:
            from textual_image.widget import TGPImage
            from PIL import Image
        except ImportError:
            return
        try:
            container = card.query_one(".cell-images", VerticalScroll)
        except Exception:
            return
        for child in list(container.children):
            child.remove()
        for img_bytes in images:
            try:
                img = Image.open(io.BytesIO(img_bytes))
                await container.mount(TGPImage(img))
            except Exception:
                pass
        container.add_class("has-images")

    def action_save(self) -> None:
        self.run_worker(self._save_notebook, exclusive=True)

    async def _save_notebook(self) -> None:
        loop = asyncio.get_event_loop()
        # Build nbformat from current cells.
        nb = nbformat.v4.new_notebook()
        scroll = self.query_one("#nb-scroll", VerticalScroll)
        cards = scroll.query(CellCard)
        for card in cards:
            if card.cell.cell_type == "code":
                cell = nbformat.v4.new_code_cell(card.get_source())
            else:
                cell = nbformat.v4.new_markdown_cell(card.get_source())
            nb.cells.append(cell)
        data = nbformat.writes(nb).encode()
        ok = await loop.run_in_executor(
            None,
            self._ssh.write_remote_file,
            self.node_name,
            self.remote_path,
            data,
        )
        if ok:
            self.notify("Notebook saved")
        else:
            self.notify("Save failed", severity="error")

    def action_interrupt(self) -> None:
        if self._kernel is not None:
            self._kernel.interrupt()
            self.notify("Interrupt sent")

    def shutdown_kernel(self) -> None:
        if self._kernel is not None:
            self._kernel.shutdown()
            self._kernel = None


def _self_check() -> None:
    cs = CellState(source="print('hi')")
    assert cs.cell_type == "code"
    assert cs.outputs == []
    assert cs.result is None
    card = CellCard.__new__(CellCard)
    card.cell = cs
    card.index = 0
    assert card.get_images() == []
    assert not card.has_images
    nb = {
        "cells": [
            {"cell_type": "code", "source": "print(1)", "outputs": [], "metadata": {}, "execution_count": None},
            {"cell_type": "markdown", "source": "# hi", "metadata": {}},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    raw = json.dumps(nb).encode()
    parsed = nbformat.reads(raw.decode(), as_version=4)
    cells = [c for c in parsed.cells if c.cell_type in ("code", "markdown")]
    assert len(cells) == 2
    assert cells[0].source == "print(1)"
    print("notebook_view self-check passed")


if __name__ == "__main__":
    _self_check()
