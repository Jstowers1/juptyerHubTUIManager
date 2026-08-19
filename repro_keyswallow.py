# Headless repro for key swallowing. Mirrors the REAL app layout:
# TabbedContent, terminal tab ACTIVE, notebook tab open with N cells
# (hidden). Types "clear" via Pilot, counts bytes reaching os.write.
# No SSH, no real kernel. Measure, do not theorize.

from __future__ import annotations

import asyncio
import os
import sys

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static, TabbedContent, TabPane

sys.path.insert(0, ".")
from jupyter_hub_tui.terminal import TerminalDisplay  # noqa: E402


def make_app(n_cells: int) -> type:
    class Repro(App):
        def compose(self) -> ComposeResult:
            with TabbedContent():
                with TabPane("Terminal", id="terminal-tab"):
                    yield TerminalDisplay(id="term")
                with TabPane("notebook.ipynb", id="nb-tab"):
                    with VerticalScroll():
                        for i in range(n_cells):
                            yield Static(f"cell {i} " + "x" * 60)

        async def on_mount(self) -> None:
            term = self.query_one("#term", TerminalDisplay)
            term.start(["cat"])
            tabs = self.query_one(TabbedContent)
            tabs.active = "terminal-tab"
            term.focus()

    return Repro


async def run_case(name: str, n_cells: int, delay_between: float) -> str:
    app_cls = make_app(n_cells)
    app = app_cls()
    async with app.run_test() as pilot:
        term = app.query_one("#term", TerminalDisplay)
        for _ in range(40):
            if term._pty_running:
                break
            await asyncio.sleep(0.05)
        assert term._pty_running, f"{name}: PTY did not start"

        state = {"count": 0, "bytes": b""}
        real_write = os.write

        def counting_write(fd, data):
            state["count"] += 1
            state["bytes"] += data if isinstance(data, bytes) else bytes(data)
            return real_write(fd, data)

        os.write = counting_write
        try:
            for ch in "clear":
                await pilot.press(ch)
                await asyncio.sleep(delay_between)
            await asyncio.sleep(0.5)
        finally:
            os.write = real_write

        wrote = state["bytes"].decode(errors="replace")
        grid_text = "".join(
            c.char for row in term._screen.grid for c in row
        )
        print(f"[{name}] cells={n_cells} delay={delay_between}s")
        print(f"  os.write calls: {state['count']}  wrote: {wrote!r}")
        print(f"  grid has 'clear': {'clear' in grid_text}")
        missing = [c for c in "clear" if c not in wrote]
        verdict = "PASS" if not missing else f"FAIL missing={missing}"
        print(f"  verdict: {verdict}")
        app.exit()
        return verdict


async def main() -> None:
    results = []
    for label, cells, delay in [
        ("no-cells", 0, 0.0),
        ("no-cells-human", 0, 0.08),
        ("50-cells", 50, 0.0),
        ("50-cells-human", 50, 0.08),
    ]:
        print(f"=== {label} ===")
        results.append(await run_case(label, cells, delay))
    print()
    print("SUMMARY:", results)


if __name__ == "__main__":
    asyncio.run(main())
