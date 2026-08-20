# Notebook viewer widget. Renders .ipynb cells, executes via RemoteKernel,
# displays images via textual-image kitty graphics protocol.

from __future__ import annotations

import asyncio
import io
import json
from dataclasses import dataclass, field

import nbformat
from rich.syntax import Syntax
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Markdown, RichLog, Static, TextArea

from .kernel_client import CellResult, RemoteKernel


@dataclass
class CellState:
    source: str
    cell_type: str = "code"
    outputs: list[dict] = field(default_factory=list)
    result: CellResult | None = None
    running: bool = False


class CellCard(Widget):
    # One cell: editor + output area. Uses Static for display,
    # swaps to TextArea only when focused for editing.

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
    CellCard.focused {
        border: round $accent;
    }
    CellCard .cell-source {
        height: auto;
        min-height: 1;
        padding: 0 1;
        color: $text;
    }
    CellCard .cell-source.markdown {
        text-style: bold;
        color: $primary;
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

    def __init__(self, cell: CellState, index: int, language: str = "python") -> None:
        super().__init__()
        self.cell = cell
        self.index = index
        self._editing = False
        self._language = language
        # Image zoom scale for this cell. 1.0 = fit container.
        self._zoom = 1.0

    def compose(self) -> ComposeResult:
        yield Static(
            f"[{self.index}] {self.cell.cell_type}",
            classes="cell-type-badge",
        )
        # Markdown cells render as rich Markdown, not raw source.
        if self.cell.cell_type == "markdown":
            yield Markdown(self.cell.source, classes="cell-source markdown")
        else:
            yield Static(
                Syntax(self.cell.source, self._language, theme="ansi_dark"),
                classes="cell-source",
            )
        output = Static("", classes="cell-output")
        yield output
        yield VerticalScroll(classes="cell-images")

    def on_mount(self) -> None:
        self._render_output()

    def _swap_to_editor(self) -> None:
        if self._editing:
            return
        try:
            # Works for both Static (code) and Markdown displays.
            src = self.query_one(".cell-source")
        except Exception:
            return
        # Normalize: kernel names like ipython3 -> python (tree-sitter builtin).
        lang = self._language.lower().replace("ipython", "python")
        lang = "python" if lang.startswith("python") else lang
        if self.cell.cell_type != "code":
            lang = None
        try:
            editor = TextArea(
                text=self.cell.source,
                language=lang,
                classes="cell-editor",
            )
        except Exception:
            editor = TextArea(text=self.cell.source, classes="cell-editor")
        src.remove()
        self.mount(editor)
        self._editing = True
        editor.focus()

    def _swap_to_display(self) -> None:
        if not self._editing:
            return
        try:
            editor = self.query_one(TextArea)
        except Exception:
            return
        self.cell.source = editor.text
        editor.remove()
        # Markdown re-renders after edit; code re-highlight.
        if self.cell.cell_type == "markdown":
            self.mount(
                Markdown(self.cell.source, classes="cell-source markdown"),
                before=".cell-output",
            )
        else:
            self.mount(
                Static(
                    Syntax(self.cell.source, self._language, theme="ansi_dark"),
                    classes="cell-source",
                ),
                before=".cell-output",
            )
        self._editing = False

    def enter_edit_mode(self) -> None:
        self._swap_to_editor()
        self.add_class("focused")

    def exit_edit_mode(self) -> None:
        self._swap_to_display()
        self.remove_class("focused")

    def get_source(self) -> str:
        if self._editing:
            try:
                return self.query_one(TextArea).text
            except Exception:
                pass
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

    def _clear_image_statics(self, container) -> None:
        # Remove mounted image Statics.
        for child in list(container.children):
            if isinstance(child, Static):
                child.remove()

    def rerender_images(self) -> None:
        # Mount images with the halfcell renderer.
        try:
            container = self.query_one(".cell-images", VerticalScroll)
        except Exception:
            return
        self._clear_image_statics(container)
        images = self.get_images()
        if not images:
            container.remove_class("has-images")
            return
        container.add_class("has-images")
        try:
            from textual_image.renderable.tgp import Image as TGPRenderable
            from PIL import Image as PILImage
        except ImportError:
            return
        for img_bytes in images:
            try:
                pil_img = PILImage.open(io.BytesIO(img_bytes))
                w, h = pil_img.size
                # Explicit TGP: auto-detect fails under Textual (stdin owned).
                # int width/height are CELLS, hard cap below 297-diacritic limit.
                try:
                    from textual_image._terminal import get_cell_size

                    cw, chh = get_cell_size() or (8, 16)
                except Exception:
                    cw, chh = 8, 16
                img_cw = w / cw
                img_ch = h / chh
                fit = min(
                    max(10, container.size.width - 2) / max(img_cw, 1),
                    max(10, container.size.height - 2) / max(img_ch, 1),
                    1.0,
                )
                cells_w = min(int(img_cw * fit), 290)
                cells_h = min(int(img_ch * fit), 290)
                renderable = TGPRenderable(pil_img, width=cells_w, height=cells_h)
                container.mount(Static(renderable, classes="img-display"))
            except Exception:
                pass

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
        height: 5;
        background: $panel;
        color: $text-muted;
        padding: 0 1;
        border-top: solid $primary;
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
        yield RichLog(id="nb-status", markup=True)

    def on_focus(self) -> None:
        # When NotebookView gets focus (e.g. via Ctrl+T), cascade into
        # the active cell's TextArea.
        self._focus_cell(self._active_cell)

    def on_mount(self) -> None:
        self.run_worker(self._load_and_start, exclusive=True)

    async def _load_and_start(self) -> None:
        from . import config as cfg

        status = self.query_one("#nb-status", RichLog)
        status.write("[yellow]Loading notebook...[/]")
        # Wait for master socket before any non-interactive SSH.
        loop = asyncio.get_event_loop()
        ready = await loop.run_in_executor(
            None, self._ssh.wait_for_master, self.node_name
        )
        if not ready:
            status.write("[red]SSH master socket not ready. Is the terminal connected?[/]")
            return
        raw = await loop.run_in_executor(
            None,
            self._ssh.read_remote_file,
            self.node_name,
            self.remote_path,
        )
        if raw is None:
            err = self._ssh._last_error or "unknown"
            status.write(f"[red]Failed to load notebook: {err}[/]")
            return
        nb = nbformat.reads(raw.decode(), as_version=4)
        self._nb = nb
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
        # Render cells first; kernel is not needed to view or edit.
        await self._render_cells()
        status.write(f"[yellow]Starting kernel...[/]")
        # Start remote kernel.
        venv_cmd = cfg.venv_activate_cmd(self._config)
        pythonpath = cfg.venv_pythonpath(self._config)
        self._kernel = RemoteKernel(
            self._ssh, self.node_name, venv_cmd, pythonpath
        )
        try:
            await loop.run_in_executor(None, self._kernel.start)
        except Exception as e:
            status.write(f"[red]Kernel failed:[/]")
            status.write(str(e))
            return
        status.write(f"[green]Kernel ready[/]  {len(self._cells)} cells")
        self.post_message(self.KernelStarted())

    async def _render_cells(self) -> None:
        scroll = self.query_one("#nb-scroll", VerticalScroll)
        scroll.remove_children()
        # Kernel language from metadata, python fallback.
        lang = "python"
        try:
            lang = self._nb.metadata["language_info"]["name"]
        except (AttributeError, KeyError, TypeError):
            pass
        # Normalize kernel names: ipython3 -> python for tree-sitter/pygments.
        lang = lang.lower().replace("ipython", "python")
        # Batch mount: one layout pass instead of N.
        cards = [
            CellCard(cell, i, language=lang)
            for i, cell in enumerate(self._cells)
        ]
        if cards:
            await scroll.mount_all(cards)
        if self._cells:
            self._focus_cell(0)

    def _focus_cell(self, idx: int) -> None:
        if not self._cells:
            return
        idx = max(0, min(idx, len(self._cells) - 1))
        # Exit edit mode on previous cell.
        scroll = self.query_one("#nb-scroll", VerticalScroll)
        cards = list(scroll.query(CellCard))
        for c in cards:
            if c._editing:
                c.exit_edit_mode()
        self._active_cell = idx
        if idx < len(cards):
            cards[idx].enter_edit_mode()
            scroll.scroll_to_widget(cards[idx])

    def _current_card(self) -> CellCard | None:
        scroll = self.query_one("#nb-scroll", VerticalScroll)
        cards = list(scroll.query(CellCard))
        if not cards:
            return None
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
        if self._kernel is None or not self._kernel.alive:
            self.notify("Kernel not ready", severity="warning")
            return
        card = self._current_card()
        if card is None:
            return
        code = card.get_source()
        card.cell.source = code
        if card.cell.cell_type != "code":
            status = self.query_one("#nb-status", RichLog)
            status.write(f"[dim]Cell {card.index} is markdown (skipped)[/]")
            if run_next:
                self._focus_cell(self._active_cell + 1)
            return
        card.set_running(True)
        status = self.query_one("#nb-status", RichLog)
        status.write(f"[yellow]Running cell {card.index}...[/]")
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self._kernel.execute, code
        )
        card.set_result(result)
        await self._refresh_card_images(card)
        if result.error:
            status.write(f"[red]Cell {card.index} error[/]")
        else:
            status.write(f"[green]Cell {card.index} done[/]  {len(result.images)} image(s)")
        if run_next:
            self._focus_cell(self._active_cell + 1)

    async def _refresh_card_images(self, card: CellCard) -> None:
        # Delegate to the card: it owns zoom state and buttons.
        card.rerender_images()

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
