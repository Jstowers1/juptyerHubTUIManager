#Regression: escape inside a vim tab must not open the quit dialog.
import asyncio

from jupyter_hub_tui.app import JupyterHubTUI
from jupyter_hub_tui.terminal import TerminalDisplay


class FakeSSH:
    class _N:
        name = "fakenode"

    active = _N()
    nodes: dict = {}

    def __init__(self):
        self.vim_cmd = ["sleep", "10"]

    def list_remote_dir(self, name, path):
        return []

    def remote_vim_command(self, name, path):
        return self.vim_cmd


async def main() -> None:
    app = JupyterHubTUI()
    app._ssh = FakeSSH()
    app._data = {"browse_paths": ["~"]}
    async with app.run_test(size=(100, 30)) as pilot:
        app._open_vim_tab("fakenode", "~/repo/file.py")
        #Let mount + call_after_refresh chain run.
        await pilot.pause(0.5)
        term = app.query_one("#term-tabs").query_one("#vim-tab-1").query_one(TerminalDisplay)
        assert term.is_mounted, "vim terminal not mounted"
        assert app.focused is term, f"focus on {app.focused!r}, not the vim terminal"
        assert term._pty_running, "vim pty not running"
        #Escape while vim runs: the terminal must swallow it.
        await pilot.press("escape")
        from textual.screen import ModalScreen
        assert not any(isinstance(s, ModalScreen) for s in app.screen_stack), \
            "escape opened the quit dialog"
        print("VIM REGRESSION OK")


asyncio.run(main())
