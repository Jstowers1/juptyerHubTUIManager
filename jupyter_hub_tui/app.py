# Main TUI application.

from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static, ListView, ListItem, Label

from . import config as cfg
from . import venv
from .git_status import status as git_status_info
from .ssh_manager import SSHManager


CSS = """
Screen {
    background: $surface;
}

#status-bar {
    dock: bottom;
    height: 1;
    background: $panel;
    color: $text;
}

#status-bar .venv-on {
    color: $success;
}

#status-bar .venv-off {
    color: $error;
}

#status-bar .node-connected {
    color: $accent;
}

#left-panel {
    width: 1fr;
    border: solid $primary;
    padding: 0 1;
}

#right-panel {
    width: 3fr;
    border: solid $primary;
    padding: 0 1;
}

.node-list {
    height: auto;
}

.node-list-label {
    color: $text-muted;
    text-style: bold;
}

#content-area {
    padding: 1 2;
}

.example-warning {
    color: $warning;
    text-style: italic;
}
"""


class NodeListItem(ListItem):
    # ListItem holding a node name for the connection panel.
    pass


class JupyterHubTUI(App):
    # Main app. Manages cluster connections, venv status, and docs.

    TITLE = "Jupyter Hub TUI"
    CSS = CSS

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("1", "connect_node('pub')", "Pub", show=False),
        Binding("2", "connect_node('cobalt')", "Cobalt", show=False),
        Binding("3", "connect_node('npx-submitter')", "NPX", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._data = cfg.load()
        self._ssh = SSHManager(self._data)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left-panel"):
                yield Label("Cluster Nodes", classes="node-list-label")
                yield ListView(id="node-list")
                yield Label("", id="ssh-command-display")
            with VerticalScroll(id="right-panel"):
                yield Static("", id="content-area")
        yield Footer()
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        self._populate_nodes()
        self._update_status()
        self._render_welcome()

    def _populate_nodes(self) -> None:
        lv = self.query_one("#node-list", ListView)
        for name, node in self._ssh.nodes.items():
            item = NodeListItem(Label(f"{name}: {node.description}"))
            item.data = name
            lv.append(item)

    def _update_status(self) -> None:
        bar = self.query_one("#status-bar", Static)
        venv_state = venv.is_active()
        venv_icon = "[green]VENV:ON[/]" if venv_state else "[red]VENV:OFF[/]"
        active = self._ssh.active
        if active:
            node_text = f"[cyan]CONNECTED:{active.name}[/]"
        else:
            node_text = "[dim]NO CONNECTION[/]"

        # Git branch and dirty state.
        repo_path = str(Path(__file__).resolve().parent.parent)
        gs = git_status_info(repo_path)
        if gs:
            dirty_text = "[red]*[/]" if gs.dirty else ""
            ahead_behind = ""
            if gs.ahead:
                ahead_behind += f" +{gs.ahead}"
            if gs.behind:
                ahead_behind += f" -{gs.behind}"
            git_text = f"  [blue]git:{gs.branch}{dirty_text}{ahead_behind}[/]"
        else:
            git_text = ""

        bar.update(f" {venv_icon}  {node_text}{git_text}")

    def _render_welcome(self) -> None:
        content = self.query_one("#content-area", Static)
        if self._data.get("_example"):
            content.update(
                "[yellow]Using config.example.json.[/]\n"
                "Copy to config.json and fill in your details.\n\n"
            )
        else:
            content.update("Select a node to connect, or press 1/2/3.\n\n")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Handle node selection from the list.
        if hasattr(event.list_view, "id") and event.list_view.id == "node-list":
            name = event.item.data
            self.action_connect_node(name)

    def action_connect_node(self, name: str) -> None:
        # Set active node and display the SSH command.
        if name not in self._ssh.nodes:
            self.notify(f"Unknown node: {name}", severity="error")
            return
        node = self._ssh.set_active(name)
        self._update_status()
        cmd_display = self.query_one("#ssh-command-display", Label)
        cmd_display.update(f"[dim]$ {self._ssh.command_str(name)}[/]")
        self.notify(f"Active node: {node.name} ({node.host})")

    def action_refresh(self) -> None:
        self._update_status()
        self.notify("Status refreshed")


def main() -> None:
    app = JupyterHubTUI()
    app.run()


if __name__ == "__main__":
    main()
