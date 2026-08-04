# Main TUI application.

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Static, ListView, ListItem, Label
from textual.widgets import Input, Button, Tree

from textual.widgets._tree import TreeNode

from . import config as cfg
from . import venv
from .git_status import status as git_status_info
from .ssh_manager import SSHManager
from .terminal import TerminalDisplay


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

#left-panel {
    width: 1fr;
    border: solid $primary;
    padding: 0 1;
}

#left-panel:focus-within {
    border: solid $accent;
}

#right-panel {
    width: 3fr;
    border: solid $primary;
    padding: 0 1;
}

#right-panel:focus-within {
    border: solid $accent;
}

#term-display {
    display: none;
    border: solid $accent;
}

#term-display:focus {
    border: double $accent;
}

.node-list-label {
    color: $text-muted;
    text-style: bold;
}

.section-label {
    color: $text-muted;
    text-style: bold;
    margin-top: 1;
}

#content-area {
    padding: 1 2;
}

#file-tree {
    height: 1fr;
    min-height: 5;
}

#node-list {
    height: auto;
    max-height: 40%;
}

#ssh-command-display {
    margin-top: 1;
}
"""


class NodeListItem(ListItem):
    pass


class JupyterHubTUI(App):

    TITLE = "Jupyter Hub TUI"
    CSS = CSS

    BINDINGS = [
        Binding("escape", "quit", "Quit"),
        Binding("ctrl+r", "refresh", "Refresh"),
        Binding("ctrl+n", "new_connection", "Dashboard"),
        Binding("ctrl+m", "show_manual", "Manual"),
        Binding("ctrl+j", "launch_jupyter", "Jupyter"),
        Binding("ctrl+k", "setup_keys", "SSH Keys"),
        Binding("ctrl+e", "edit_node", "Edit Node"),
        Binding("tab", "focus_left", "Nodes"),
        Binding("shift+tab", "focus_terminal", "Terminal"),
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
                yield Label("Files", classes="section-label")
                yield Tree("root", id="file-tree")
            with VerticalScroll(id="right-panel"):
                yield Static("", id="content-area")
                yield TerminalDisplay(id="term-display")
        yield Footer()
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        self._populate_nodes()
        self._update_status()
        self._render_welcome()
        self._populate_file_tree()
        self._node_names = list(self._ssh.nodes.keys())

    @property
    def _term(self) -> TerminalDisplay:
        return self.query_one("#term-display", TerminalDisplay)

    def on_key(self, event) -> None:
        # Forward keys to terminal when it is running.
        if self._term.is_running:
            self._term.send_key(event.key, event.character)
            event.prevent_default()
            event.stop()
            return
        # Quick-connect: number keys match nodes by index.
        if event.character and event.character.isdigit():
            idx = int(event.character) - 1
            if 0 <= idx < len(self._node_names):
                self._start_ssh(self._node_names[idx])
                event.prevent_default()
                event.stop()

    def _start_ssh(self, name: str) -> None:
        # Stop old session, start new one in the same widget.
        if name not in self._ssh.nodes:
            self.notify(f"Unknown node: {name}", severity="error")
            return
        node = self._ssh.set_active(name)
        self._update_status()
        cmd_display = self.query_one("#ssh-command-display", Label)
        cmd_display.update(f"[dim]$ {self._ssh.command_str(name)}[/]")
        # Hide content, show terminal.
        self.query_one("#content-area", Static).display = False
        term = self._term
        term.stop()
        term.reset()
        term.display = True
        cmd = self._ssh.launch(name)
        term.start(cmd)
        term.focus()
        self.notify(f"Connecting to: {node.name} ({node.host})")

    def on_terminal_display_exited(self, event: TerminalDisplay.Exited) -> None:
        # SSH process exited: restore dashboard.
        term = self._term
        term.display = False
        content = self.query_one("#content-area", Static)
        content.display = True
        content.update("[yellow]SSH session ended.[/]")
        content.focus()

    def action_new_connection(self) -> None:
        # Stop terminal and return to dashboard.
        self._term.stop()
        self._term.display = False
        content = self.query_one("#content-area", Static)
        content.display = True
        content.focus()

    def action_focus_left(self) -> None:
        self.query_one("#node-list", ListView).focus()

    def action_focus_terminal(self) -> None:
        term = self._term
        if term.display:
            term.focus()
        else:
            self.query_one("#content-area", Static).focus()

    def _populate_nodes(self) -> None:
        lv = self.query_one("#node-list", ListView)
        lv.clear()
        for name, node in self._ssh.nodes.items():
            item = NodeListItem(Label(f"{name}: {node.description}"))
            item.data = name
            lv.append(item)

    def _populate_file_tree(self) -> None:
        tree = self.query_one("#file-tree", Tree)
        tree.clear()
        repo_path = cfg.git_repo_path(self._data)
        if repo_path == ".":
            repo_path = str(Path(__file__).resolve().parent.parent)
        root = Path(repo_path).expanduser()
        if not root.is_dir():
            tree.root.add_leaf("[red]Repo path not found[/]")
            return
        self._add_tree_node(tree.root, root, depth=2)

    def _add_tree_node(self, node: TreeNode, path: Path, depth: int) -> None:
        # ponytail: fixed depth 2, no lazy loading.
        if depth <= 0:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except PermissionError:
            return
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                child = node.add(entry.name, allow_expand=True)
                self._add_tree_node(child, entry, depth - 1)
            else:
                node.add_leaf(entry.name)

    def _update_status(self) -> None:
        bar = self.query_one("#status-bar", Static)
        venv_state = venv.is_active()
        venv_icon = "[green]VENV:ON[/]" if venv_state else "[red]VENV:OFF[/]"
        active = self._ssh.active
        if active:
            node_text = f"[cyan]CONNECTED:{active.name}[/]"
        else:
            node_text = "[dim]NO CONNECTION[/]"
        repo_path = cfg.git_repo_path(self._data)
        if repo_path == ".":
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
        if hasattr(event.list_view, "id") and event.list_view.id == "node-list":
            self._start_ssh(event.item.data)

    def action_refresh(self) -> None:
        self._update_status()
        self._populate_file_tree()
        self.notify("Status refreshed")

    def action_show_manual(self) -> None:
        manual_path = Path(__file__).resolve().parent.parent / "docs" / "manual.md"
        content = self.query_one("#content-area", Static)
        if not manual_path.exists():
            content.update("[red]Manual not found at docs/manual.md[/]")
            return
        content.update(manual_path.read_text())

    def action_launch_jupyter(self) -> None:
        from .jupyter import launch
        if not self._ssh.active:
            self.notify("No active node. Select a node first.", severity="warning")
            return
        settings = cfg.jupyter_settings(self._data)
        port = settings.get("port", 8888)
        remote_venv = cfg.remote_venv_path(self._data) or "~/.venv/icetop"
        self.notify(f"Launching Jupyter on {self._ssh.active.name}...")
        launch(self._ssh, self._ssh.active.name, port, remote_venv)
        self.notify(f"Jupyter tunneling on localhost:{port}")

    def action_setup_keys(self) -> None:
        # Run key setup in the embedded terminal so passwords work.
        self.query_one("#content-area", Static).display = False
        term = self._term
        term.stop()
        term.reset()
        term.display = True
        term.start(self._ssh.setup_keys_command())
        term.focus()
        self.notify("Setting up SSH keys. Enter passwords as prompted.")

    def action_edit_node(self) -> None:
        if not self._ssh.active:
            self.notify("No active node. Select a node first.", severity="warning")
            return
        self.push_screen(NodeEditScreen(self._ssh.active, self._data, self._on_node_saved))

    def _on_node_saved(self) -> None:
        self._data = cfg.load()
        self._ssh = SSHManager(self._data)
        self._populate_nodes()
        self._update_status()
        self._populate_file_tree()
        self._node_names = list(self._ssh.nodes.keys())
        self.notify("Node saved to config.json")


class NodeEditScreen(ModalScreen):

    BINDINGS = [Binding("escape", "app.pop_screen", "Cancel", show=False)]

    DEFAULT_CSS = """
    NodeEditScreen {
        align: center middle;
    }
    #edit-dialog {
        width: 60;
        height: auto;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    #edit-dialog Label {
        width: 8;
        text-style: bold;
    }
    #edit-dialog Input {
        width: 1fr;
    }
    #edit-dialog .row {
        height: 3;
    }
    #edit-dialog Button {
        margin: 0 1;
    }
    #edit-dialog #edit-title {
        width: 100%;
        text-style: bold;
        margin-bottom: 1;
    }
    """

    def __init__(self, node, data, on_save):
        super().__init__()
        self._node = node
        self._data = data
        self._on_save = on_save

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label(f"Edit Node: {self._node.name}", id="edit-title")
            with Horizontal(classes="row"):
                yield Label("Host")
                yield Input(value=self._node.host, id="edit-host")
            with Horizontal(classes="row"):
                yield Label("User")
                yield Input(value=self._node.user, id="edit-user")
            with Horizontal(classes="row"):
                yield Label("Port")
                yield Input(value=str(self._node.port), id="edit-port")
            with Horizontal(classes="row"):
                yield Label("Proxy")
                yield Input(value=self._node.proxy or "", id="edit-proxy")
            with Horizontal(classes="row"):
                yield Button("Save", id="edit-save", variant="success")
                yield Button("Cancel", id="edit-cancel", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "edit-cancel":
            self.app.pop_screen()
            return
        if event.button.id == "edit-save":
            host = self.query_one("#edit-host", Input).value
            user = self.query_one("#edit-user", Input).value
            port = int(self.query_one("#edit-port", Input).value or 22)
            proxy = self.query_one("#edit-proxy", Input).value or None
            cfg.update_node(self._data, self._node.name,
                            host=host, user=user, port=port, proxy=proxy)
            cfg.save(self._data)
            self.app.pop_screen()
            self._on_save()


def main() -> None:
    app = JupyterHubTUI()
    app.run()


if __name__ == "__main__":
    main()
