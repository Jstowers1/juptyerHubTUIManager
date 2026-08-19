# Main TUI application.

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Click
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Static, ListView, ListItem, Label
from textual.widgets import Input, Button, Tree, RichLog
from textual.widgets import TabbedContent, TabPane

from . import config as cfg
from .notebook_view import CellCard, NotebookView
from .ssh_manager import SSHManager
from .terminal import TerminalDisplay


CSS = """
Screen {
    background: $surface;
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
    padding: 0;
}

#right-panel:focus-within {
    border: solid $accent;
}

#content-area {
    height: 1fr;
    padding: 1 2;
}

#term-tabs {
    height: 1fr;
}

#term-tabs ContentTabs {
    dock: bottom;
    background: $panel;
}

#term-tabs TabPane {
    height: 1fr;
}

#term-tabs TerminalDisplay {
    height: 1fr;
    background: #1d1f21;
}

#help-panel {
    display: none;
    height: 1fr;
    max-height: 100%;
    background: $surface;
    padding: 0 1;
    overflow-y: auto;
    border: solid $primary;
}

#help-panel:focus {
    border: solid $accent;
    background: $boost;
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


class FocusableStatic(Static):
    # Static has can_focus=False. We need it focusable for Tab to work.
    can_focus = True


class FocusableLog(RichLog):
    can_focus = True

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("markup", True)
        super().__init__(*args, **kwargs)


def _download_fetch(ssh, node_name: str, remote_path: str) -> str:
    # Pull a remote file/folder into ./downloads. Sync, run in a thread.
    local_root = Path("downloads")
    local_root.mkdir(exist_ok=True)

    def walk(rpath: str, lpath: Path) -> list[str]:
        got = []
        try:
            entries = ssh.list_remote_dir(node_name, rpath)
        except Exception:
            return got
        for e in entries:
            r = f"{rpath}/{e['name']}"
            l = lpath / e["name"]
            if e["is_dir"]:
                l.mkdir(parents=True, exist_ok=True)
                got.extend(walk(r, l))
            else:
                data = ssh.read_remote_file(node_name, r)
                if data is not None:
                    l.write_bytes(data)
                    got.append(str(l))
        return got

    target = Path(remote_path.lstrip("/"))
    if target.suffix or "." in target.name:
        data = ssh.read_remote_file(node_name, remote_path)
        if data is None:
            return "download failed"
        (local_root / target.name).write_bytes(data)
        return f"saved downloads/{target.name}"
    (local_root / target.name).mkdir(parents=True, exist_ok=True)
    got = walk(remote_path, local_root / target.name)
    return f"saved {len(got)} files to downloads/{target.name}"


class JupyterHubTUI(App):

    TITLE = "Jupyter Hub TUI"
    SUB_TITLE = "NO CONNECTION"
    CSS = CSS

    def format_title(self, title: str, sub_title: str):
        from textual.content import Content
        if sub_title:
            return Content.assemble(
                Content(title),
                (" — ", "dim"),
                Content.from_markup(sub_title),
            )
        return Content(title)

    # Terminal.on_key intercepts escape hatches during SSH.
    # These bindings handle dashboard mode only.
    # Ctrl+e/r/k/s/i handled by NotebookView when notebook is active.
    BINDINGS = [
        Binding("ctrl+m", "show_manual", "Manual"),
        Binding("ctrl+h", "show_help", "Help"),
        Binding("ctrl+g", "git_picker", "Git Repo"),
        Binding("ctrl+b", "git_branch", "Git Branch"),
        Binding("ctrl+backslash", "toggle_sidebar", "Sidebar", priority=True),
        Binding("ctrl+t", "toggle_term_focus", "Focus", priority=True),
        Binding("ctrl+w", "close_tab", "Close Tab"),
        Binding("ctrl+o", "activate_venv", "Venv"),
        Binding("ctrl+left", "prev_tab", "Prev Tab", show=False, priority=True),
        Binding("ctrl+right", "next_tab", "Next Tab", show=False, priority=True),
        Binding("ctrl+n", "disconnect", show=False),
        Binding("escape", "confirm_quit", show=False),
        Binding("tab", "cycle_focus", show=False),
        Binding("1", "quick_connect(0)", show=False),
        Binding("2", "quick_connect(1)", show=False),
        Binding("3", "quick_connect(2)", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._data = cfg.load()
        self._ssh = SSHManager(self._data)
        self._nb_counter = 0
        self._notebook_views: list[NotebookView] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="left-panel"):
                yield FocusableLog(id="help-panel")
                yield Label("Cluster Nodes", classes="node-list-label")
                yield ListView(id="node-list")
                yield Label("", id="ssh-command-display")
                yield Label("Files", classes="section-label")
                yield Tree("root", id="file-tree")
            with Vertical(id="right-panel"):
                yield FocusableStatic("", id="content-area")
                with TabbedContent(id="term-tabs"):
                    with TabPane("Terminal", id="terminal-tab"):
                        yield TerminalDisplay(id="term-display")
        yield Footer()

    def on_mount(self) -> None:
        self._populate_nodes()
        self._update_status()
        self._render_welcome()
        self._populate_file_tree()
        self._node_names = list(self._ssh.nodes.keys())
        self._populate_help()
        # Hide tabs until SSH starts.
        self.query_one("#term-tabs").display = False

    def _populate_help(self) -> None:
        hp = self.query_one("#help-panel", FocusableLog)
        hp.clear()
        hp.write("[bold]Keybindings[/]")
        hp.write("")
        hp.write("[cyan]Navigation[/]")
        hp.write("  Tab          Cycle sidebar widgets")
        hp.write("  Ctrl+T       Toggle terminal / sidebar (SSH)")
        hp.write("  Ctrl+\\       Toggle sidebar")
        hp.write("  Ctrl+H       Toggle this help panel")
        hp.write("  1-3          Quick-connect to node")
        hp.write("")
        hp.write("[cyan]Cluster[/]")
        hp.write("  Ctrl+K       Set up SSH keys")
        hp.write("  Ctrl+M       View cluster manual")
        hp.write("  Ctrl+R       Refresh status / file tree")
        hp.write("  Ctrl+O       Toggle remote venv (from config)")
        hp.write("")
        hp.write("[cyan]Git[/]")
        hp.write("  Ctrl+G       Pick git repo path (remote)")
        hp.write("  Ctrl+B       Git screen: log, branches, fetch, pull, checkout")
        hp.write("    f          Fetch (inside git screen)")
        hp.write("    p          Pull (inside git screen)")
        hp.write("    Enter      Checkout branch")
        hp.write("")
        hp.write("[cyan]Notebooks[/]")
        hp.write("  Enter        Open .ipynb in new tab (custom notebook viewer)")
        hp.write("  Ctrl+W       Close notebook tab")
        hp.write("  Ctrl+Left    Previous tab")
        hp.write("  Ctrl+Right   Next tab")
        hp.write("")
        hp.write("[cyan]Notebook Cells[/]")
        hp.write("  Ctrl+E       Run cell")
        hp.write("  Ctrl+R       Run cell, move to next")
        hp.write("  Ctrl+K       Move to previous cell")
        hp.write("  Ctrl+J       Move to next cell")
        hp.write("  Ctrl+S       Save notebook")
        hp.write("  Ctrl+I       Interrupt kernel")
        hp.write("")
        hp.write("[cyan]Config[/]")
        hp.write("  Ctrl+E       Edit active node")
        hp.write("")
        hp.write("  Esc          Quit")

    @property
    def _term(self) -> TerminalDisplay:
        return self.query_one("#term-display", TerminalDisplay)

    def on_unmount(self) -> None:
        for nb in self._notebook_views:
            try:
                nb.shutdown_kernel()
            except Exception:
                pass

    @property
    def _content(self) -> FocusableStatic:
        return self.query_one("#content-area", FocusableStatic)

    # --- SSH session mode ---
    # Terminal can_focus defaults to False. _try_start sets it True
    # when SSH starts. stop/_handle_exit set it back to False.

    def _start_ssh(self, name: str) -> None:
        if name not in self._ssh.nodes:
            self.notify(f"Unknown node: {name}", severity="error")
            return
        node = self._ssh.set_active(name)
        self.notify(f"Connecting to: {node.name} ({node.host})")
        cmd_display = self.query_one("#ssh-command-display", Label)
        cmd_display.update(f"[dim]$ {self._ssh.command_str(name)}[/]")
        self._content.display = False
        self.query_one("#term-tabs").display = True
        term = self._term
        term.stop()
        term.reset()
        cmd = self._ssh.launch(name)
        term.start(cmd)
        term.focus()
        self.run_worker(self._bg_update_after_connect(), exclusive=True)

    async def _bg_update_after_connect(self) -> None:
        import asyncio
        loop = asyncio.get_event_loop()
        active = self._ssh.active
        if not active:
            return
        # Poll until background SSH works (agent key available).
        for _ in range(30):
            if await loop.run_in_executor(
                None, self._ssh.check_ssh_ready, active.name
            ):
                break
            await asyncio.sleep(1)
        else:
            self.notify("SSH connection timed out.", severity="warning")
            return
        repo_path = cfg.git_repo_path(self._data)
        file_entries = await loop.run_in_executor(
            None, self._fetch_file_entries, active.name)
        status_info = await loop.run_in_executor(
            None, self._fetch_status_info, active.name, repo_path)
        self._apply_file_tree(file_entries)
        self._apply_status_bar(status_info)

    def _fetch_file_entries(self, node_name: str) -> list[dict]:
        browse = cfg.browse_path(self._data)
        try:
            return self._ssh.list_remote_dir(node_name, browse)
        except Exception:
            return []

    def _fetch_status_info(self, node_name: str, repo_path: str) -> dict:
        git_porcelain = ""
        if repo_path != ".":
            try:
                git_porcelain = self._ssh.remote_git_status(node_name, repo_path)
            except Exception:
                pass
        return {"git_porcelain": git_porcelain}

    def _apply_file_tree(self, entries: list[dict]) -> None:
        active = self._ssh.active
        if not active:
            return
        tree = self.query_one("#file-tree", Tree)
        tree.clear()
        browse = cfg.browse_path(self._data)
        tree.root.set_label(f"{active.name}:{browse}")
        tree.root.data = {"node": active.name, "path": browse}
        if not entries:
            tree.root.add_leaf("[dim](empty)[/]")
        for e in entries:
            tree.root.add(e["name"], allow_expand=e["is_dir"])
        tree.root.expand()

    def _apply_status_bar(self, info: dict) -> None:
        active = self._ssh.active
        if not active:
            return
        git_text = self._parse_git_status(info["git_porcelain"])
        self.sub_title = f"[cyan]{active.name}[/]{git_text}"

    def on_terminal_display_exited(self, event: TerminalDisplay.Exited) -> None:
        if event.terminal_display.id != "term-display":
            return
        self._ssh._active = None
        self.query_one("#term-tabs").display = False
        self._content.display = True
        self._content.update("[yellow]SSH session ended.[/]")
        self._content.focus()
        self._update_status()
        self._populate_file_tree()

    # --- Actions ---

    def action_quick_connect(self, idx: int) -> None:
        if 0 <= idx < len(self._node_names):
            self._start_ssh(self._node_names[idx])

    def action_toggle_sidebar(self) -> None:
        left = self.query_one("#left-panel")
        left.display = not left.display
        self._active_term()._resize_pty()

    def _active_term(self) -> TerminalDisplay:
        # Return the TerminalDisplay in the currently active tab.
        tabs = self.query_one("#term-tabs", TabbedContent)
        if not tabs.active:
            return self._term
        try:
            pane = tabs.get_pane(tabs.active)
            return pane.query_one(TerminalDisplay)
        except Exception:
            return self._term

    def action_toggle_term_focus(self) -> None:
        tabs = self.query_one("#term-tabs", TabbedContent)
        if tabs.active and tabs.active != "terminal-tab":
            if self.focused and self.focused.id != "file-tree":
                self.query_one("#file-tree", Tree).focus()
            else:
                pane = tabs.get_pane(tabs.active)
                try:
                    nb = pane.query_one(NotebookView)
                    nb._focus_cell(nb._active_cell)
                except Exception:
                    pane.focus()
            return
        term = self._active_term()
        if self.focused is term:
            term.can_focus = False
            self.query_one("#file-tree", Tree).focus()
        else:
            term.can_focus = True
            term.focus()

    def action_activate_venv(self) -> None:
        if not self._ssh.active:
            self.notify("No active SSH session.", severity="warning")
            return
        cmd = cfg.venv_activate_cmd(self._data)
        if not cmd:
            self.notify("No venv.activate_cmd in config.", severity="warning")
            return
        term = self._active_term()
        if not term.pty_active:
            self.notify("Terminal not active.", severity="warning")
            return
        term.send_input(cmd + "\n")
        self.notify("Venv toggle sent")

    def action_close_tab(self) -> None:
        tabs = self.query_one("#term-tabs", TabbedContent)
        active = tabs.active
        if not active or active == "terminal-tab":
            self.notify("Can't close terminal tab.", severity="warning")
            return
        try:
            pane = tabs.get_pane(active)
            nb = pane.query_one(NotebookView)
            nb.shutdown_kernel()
        except Exception:
            pass
        tabs.remove_pane(active)
        tabs.active = "terminal-tab"
        self._term.resume_polling()
        self._term.focus()

    def _focus_active_pane(self) -> None:
        tabs = self.query_one("#term-tabs", TabbedContent)
        if not tabs.active or tabs.active == "terminal-tab":
            self._term.can_focus = True
            self._term.focus()
            return
        pane = None
        try:
            pane = tabs.get_pane(tabs.active)
            nb = pane.query_one(NotebookView)
            nb._focus_cell(nb._active_cell)
        except Exception:
            if pane:
                pane.focus()

    def action_prev_tab(self) -> None:
        tabs = self.query_one("#term-tabs", TabbedContent)
        if not tabs.display:
            return
        ids = [p.id for p in tabs.query(TabPane)]
        if len(ids) <= 1:
            return
        i = ids.index(tabs.active) if tabs.active in ids else 0
        new_id = ids[(i - 1) % len(ids)]
        tabs.active = new_id
        self._sync_term_polling(new_id)
        self._focus_active_pane()

    def action_next_tab(self) -> None:
        tabs = self.query_one("#term-tabs", TabbedContent)
        if not tabs.display:
            return
        ids = [p.id for p in tabs.query(TabPane)]
        if len(ids) <= 1:
            return
        i = ids.index(tabs.active) if tabs.active in ids else 0
        new_id = ids[(i + 1) % len(ids)]
        tabs.active = new_id
        self._sync_term_polling(new_id)
        self._focus_active_pane()

    def _sync_term_polling(self, active_tab_id: str | None) -> None:
        if active_tab_id == "terminal-tab":
            self._term.resume_polling()
        else:
            self._term.pause_polling()

    def action_cycle_focus(self) -> None:
        # Tab: Dashboard cycles left panel <-> content.
        # During SSH, Tab does nothing (terminal swallows it).
        if self._term.pty_active:
            return
        focused = self.focused
        left = self.query_one("#left-panel")
        if focused is not None and left in focused.ancestors_with_self:
            self._content.focus()
        else:
            self.query_one("#node-list", ListView).focus()

    def action_show_help(self) -> None:
        help_panel = self.query_one("#help-panel")
        help_panel.display = not help_panel.display
        self._active_term()._resize_pty()

    def action_confirm_quit(self) -> None:
        # Escape must never kill the app directly; ask first.
        self.push_screen(QuitScreen())

    def action_git_picker(self) -> None:
        if not self._ssh.active:
            self.notify("No active node.", severity="warning")
            return
        self.push_screen(GitPickerScreen(self._data, self._ssh, self._on_git_saved))

    def _on_git_saved(self) -> None:
        self._data = cfg.load()
        self._populate_file_tree()
        self._update_status()
        self.notify("Git repo path saved")

    def action_git_branch(self) -> None:
        if not self._ssh.active:
            self.notify("No active node.", severity="warning")
            return
        repo_path = cfg.git_repo_path(self._data)
        if repo_path == ".":
            self.notify("Set git repo path first (Ctrl+G).", severity="warning")
            return
        self.push_screen(GitBranchScreen(self._ssh, repo_path, self._on_git_action))

    def _on_git_action(self) -> None:
        self._update_status()
        self._populate_file_tree()

    def action_connect_node(self, name: str) -> None:
        self._start_ssh(name)

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
        tree.root.set_label("Files")
        active = self._ssh.active
        if not active:
            tree.root.add_leaf("[dim]Connect to browse files[/]")
            return
        browse = cfg.browse_path(self._data)
        tree.root.set_label(f"{active.name}:{browse}")
        tree.root.data = {"node": active.name, "path": browse}
        try:
            entries = self._ssh.list_remote_dir(active.name, browse)
        except Exception:
            tree.root.add_leaf("[red]SSH connection failed[/]")
            return
        if not entries:
            tree.root.add_leaf("[dim](empty)[/]")
        for e in entries:
            tree.root.add(e["name"], allow_expand=e["is_dir"])
        tree.root.expand()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        tree = self.query_one("#file-tree", Tree)
        if node is tree.root:
            return
        root = tree.root
        if not root.data:
            return
        if len(node.children) > 0:
            node.remove_children()
        labels = []
        cur = node
        while cur is not None and cur is not root:
            labels.append(str(cur.label))
            cur = cur.parent
        labels.reverse()
        node_name = root.data["node"]
        full_path = root.data["path"] + "/" + "/".join(labels)
        try:
            entries = self._ssh.list_remote_dir(node_name, full_path)
        except Exception:
            return
        for e in entries:
            node.add(e["name"], allow_expand=e["is_dir"])

    def _parse_git_status(self, porcelain: str) -> str:
        if not porcelain:
            return ""
        first_line = porcelain.splitlines()[0]
        branch = ""
        if "..." in first_line:
            branch = first_line.split("...")[0].replace("## ", "")
        elif "No commits yet" not in first_line:
            branch = first_line.replace("## ", "").split("...")[0]
        dirty = any(
            not line.startswith("##") for line in porcelain.splitlines()
        )
        dirty_text = "[red]*[/]" if dirty else ""
        return f"  [bright_cyan]git:{branch}{dirty_text}[/]"

    def _update_status(self) -> None:
        active = self._ssh.active
        if not active:
            self.sub_title = "[dim]NO CONNECTION[/]"
            return
        repo_path = cfg.git_repo_path(self._data)
        git_text = ""
        if repo_path != ".":
            git_text = self._parse_git_status(
                self._ssh.remote_git_status(active.name, repo_path) or "")
        self.sub_title = f"[cyan]{active.name}[/]{git_text}"

    def _render_welcome(self) -> None:
        if self._data.get("_example"):
            self._content.update(
                "[yellow]Using config.example.json.[/]\n"
                "Copy to config.json and fill in your details.\n\n"
            )
        else:
            self._content.update("Select a node to connect, or press 1/2/3.\n\n")
        self._content.focus()

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.control.id != "file-tree":
            return
        node = event.node
        tree = event.control
        if node is tree.root:
            return
        label = str(node.label)
        if not label.endswith(".ipynb"):
            return
        root = tree.root
        if not root.data:
            return
        labels = []
        cur = node
        while cur is not None and cur is not root:
            labels.append(str(cur.label))
            cur = cur.parent
        labels.reverse()
        node_name = root.data["node"]
        full_path = root.data["path"] + "/" + "/".join(labels)
        self._open_notebook(node_name, full_path)

    async def on_click(self, event: Click) -> None:
        # Right-click: file tree node or notebook image -> downloads/.
        if event.button != 3:
            return
        w = event.widget
        if w is not None and "img-display" in getattr(w, "classes", set()):
            event.stop()
            self._save_clicked_image(w)
            return
        hit = self._tree_node_path(event)
        if hit is None:
            return
        node_name, remote_path = hit
        event.stop()
        self.notify(f"downloading {remote_path.rsplit('/', 1)[-1]}...")
        await self._download_remote(node_name, remote_path)

    def _save_clicked_image(self, img_widget) -> None:
        # Save the right-clicked cell image into downloads/.
        card = img_widget.ancestors.with_type(CellCard)
        idx = list(card.query(".img-display")).index(img_widget)
        data = card.get_images()[idx]
        target = Path("downloads")
        target.mkdir(exist_ok=True)
        n = 1
        out = target / f"image-{n}.png"
        while out.exists():
            n += 1
            out = target / f"image-{n}.png"
        out.write_bytes(data)
        self.notify(f"saved {out}")

    def _tree_node_path(self, event: Click) -> tuple[str, str] | None:
        # Resolve a right-clicked file-tree node to (node, remote_path).
        tree = event.control
        if not isinstance(tree, Tree) or tree.id != "file-tree":
            return None
        n = tree._get_node(event.y)
        if n is None or n is tree.root or not tree.root.data:
            return None
        labels = []
        cur = n
        while cur is not None and cur is not tree.root:
            labels.append(str(cur.label))
            cur = cur.parent
        labels.reverse()
        return (
            tree.root.data["node"],
            tree.root.data["path"] + "/" + "/".join(labels),
        )

    async def _download_remote(self, node_name: str, remote_path: str) -> None:
        # Pull a file or whole folder into ./downloads off the UI thread.
        try:
            msg = await asyncio.to_thread(
                _download_fetch, self._ssh, node_name, remote_path
            )
            self.notify(msg)
        except Exception as e:
            self.notify(f"download failed: {e}", severity="error")

    def _open_notebook(self, node_name: str, notebook_path: str) -> None:
        if not self._ssh.active:
            self.notify("No active SSH session.", severity="warning")
            return
        basename = notebook_path.rsplit("/", 1)[-1]
        self._nb_counter += 1
        tab_id = f"nb-tab-{self._nb_counter}"
        tabs = self.query_one("#term-tabs", TabbedContent)
        nb_view = NotebookView(
            remote_path=notebook_path,
            node_name=node_name,
            ssh=self._ssh,
            config=self._data,
        )
        self._notebook_views.append(nb_view)
        pane = TabPane(basename, nb_view, id=tab_id)
        tabs.add_pane(pane)
        tabs.active = tab_id
        self._term.pause_polling()
        nb_view.focus()
        self.notify(f"Opening {basename}...")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if hasattr(event.list_view, "id") and event.list_view.id == "node-list":
            self._start_ssh(event.item.data)

    def action_refresh(self) -> None:
        self._update_status()
        self._populate_file_tree()
        self.notify("Status refreshed")

    def action_show_manual(self) -> None:
        manual_path = Path(__file__).resolve().parent.parent / "docs" / "manual.md"
        if not manual_path.exists():
            self._content.update("[red]Manual not found at docs/manual.md[/]")
            return
        self._content.update(manual_path.read_text())

    def action_setup_keys(self) -> None:
        self._content.display = False
        self.query_one("#term-tabs").display = True
        term = self._term
        term.stop()
        term.reset()
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


class QuitScreen(ModalScreen):
    # Escape confirmation. Yes exits, anything else cancels.

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Cancel", show=False),
        Binding("y", "quit_now", "Quit", show=False),
    ]

    DEFAULT_CSS = """
    QuitScreen {
        align: center middle;
    }
    #quit-dialog {
        width: 44;
        height: auto;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="quit-dialog"):
            yield Static("Quit JupyterHub TUI?")
            yield Static("[dim]press y to exit, press esc/n to stay[/]")

    def action_quit_now(self) -> None:
        self.app.exit()

    def on_key(self, event) -> None:
        # Any non-y key cancels. Swallow everything so it
        # never leaks to the dashboard behind the modal.
        if event.key != "y":
            event.stop()
            self.app.pop_screen()


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


class GitPickerScreen(ModalScreen):
    # Pick a directory on the REMOTE filesystem. Saves to config.git.repo_path.

    BINDINGS = [Binding("escape", "app.pop_screen", "Cancel", show=False)]

    DEFAULT_CSS = """
    GitPickerScreen {
        align: center middle;
    }
    #git-dialog {
        width: 70;
        height: auto;
        max-height: 80%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    #git-dialog #dir-tree {
        height: 15;
    }
    #git-dialog #current-path {
        color: $text-muted;
        margin-bottom: 1;
    }
    #git-dialog Input {
        margin-bottom: 1;
    }
    #git-dialog Button {
        margin: 0 1;
    }
    """

    def __init__(self, data, ssh, on_save):
        super().__init__()
        self._data = data
        self._ssh = ssh
        self._on_save = on_save
        self._root = "~"
        self._node = ssh.active.name if ssh.active else None

    def compose(self) -> ComposeResult:
        with Vertical(id="git-dialog"):
            yield Label("Pick Git Repo Path (Remote)", id="edit-title")
            yield Label(self._root, id="current-path")
            tree = Tree(self._root, id="dir-tree")
            yield tree
            yield Input(value=self._root, id="git-path-input")
            with Horizontal():
                yield Button("Save", id="git-save", variant="success")
                yield Button("Cancel", id="git-cancel", variant="error")

    def on_mount(self) -> None:
        self._populate_tree()

    def _node_path(self, node) -> str:
        # Compute full remote path from root + tree labels.
        tree = self.query_one("#dir-tree", Tree)
        labels = []
        cur = node
        while cur is not None and cur is not tree.root:
            labels.append(str(cur.label))
            cur = cur.parent
        labels.reverse()
        if labels:
            return self._root + "/" + "/".join(labels)
        return self._root

    def _populate_tree(self) -> None:
        tree = self.query_one("#dir-tree", Tree)
        tree.clear()
        tree.root.set_label(self._root)
        if not self._node:
            tree.root.add_leaf("[red]No active SSH connection[/]")
            return
        try:
            entries = self._ssh.list_remote_dir(self._node, self._root)
        except Exception:
            tree.root.add_leaf("[red]SSH connection failed[/]")
            return
        for e in entries:
            if e["is_dir"]:
                tree.root.add(e["name"], allow_expand=True)
        tree.root.expand()

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        if event.node is self.query_one("#dir-tree", Tree).root:
            return
        full_path = self._node_path(event.node)
        try:
            entries = self._ssh.list_remote_dir(self._node, full_path)
        except Exception:
            return
        for e in entries:
            if e["is_dir"]:
                event.node.add(e["name"], allow_expand=True)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        tree = self.query_one("#dir-tree", Tree)
        if event.node is tree.root:
            return
        full_path = self._node_path(event.node)
        self.query_one("#current-path", Label).update(full_path)
        self.query_one("#git-path-input", Input).value = full_path

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "git-cancel":
            self.app.pop_screen()
            return
        if event.button.id == "git-save":
            path = self.query_one("#git-path-input", Input).value
            cfg.set_git_repo_path(self._data, path)
            cfg.save(self._data)
            self.app.pop_screen()
            self._on_save()


class GitBranchScreen(ModalScreen):
    # Unified git screen: branch, status, log, actions.

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Close", show=False),
        Binding("f", "fetch", "Fetch", show=False),
        Binding("p", "pull", "Pull", show=False),
        Binding("c", "checkout_prompt", "Checkout", show=False),
    ]

    DEFAULT_CSS = """
    GitBranchScreen {
        align: center middle;
    }
    #branch-dialog {
        width: 80;
        max-height: 85%;
        border: solid $primary;
        background: $surface;
        padding: 1 2;
    }
    #branch-dialog #git-output {
        height: 1fr;
        max-height: 20;
        min-height: 5;
    }
    #branch-dialog #git-log {
        height: 1fr;
        max-height: 10;
        min-height: 3;
    }
    #branch-dialog .git-section {
        color: $text-muted;
        text-style: bold;
        margin-top: 2;
    }
    #branch-dialog #branch-list {
        height: auto;
        max-height: 12;
        margin-top: 1;
    }
    #branch-dialog #branch-diff {
        height: auto;
        max-height: 10;
        margin-top: 1;
        padding-top: 1;
        border-top: solid $primary-darken-2;
    }
    #branch-dialog #git-status {
        height: auto;
        max-height: 8;
    }
    """

    def __init__(self, ssh, repo_path, on_close):
        super().__init__()
        self._ssh = ssh
        self._repo = repo_path
        self._on_close = on_close

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="branch-dialog"):
            yield Label(f"Git: {self._repo}", id="branch-title")
            yield Label("Status", classes="git_section")
            yield Static("", id="git-status")
            yield Label("Recent commits", classes="git_section")
            yield Static("", id="git-log")
            yield Label("Branches", classes="git_section")
            yield ListView(id="branch-list")
            yield Static("", id="branch-diff")
            yield Static(
                "[dim]f=fetch  p=pull  enter=checkout  esc=close[/]",
                id="branch-hints",
            )
            yield Static("", id="git-output")

    def on_mount(self) -> None:
        self._refresh()

    def on_list_view_highlighted(self, event) -> None:
        # Diff preview: files that differ from the main branch.
        if getattr(event, "list_view", None) is None or event.list_view.id != "branch-list":
            return
        item = event.item
        if item is None or not getattr(item, "data", None):
            self.query_one("#branch-diff", Static).update("")
            return
        branch = item.data
        node = self._ssh.active.name
        diff = self._ssh.remote_git_diff_branch(node, self._repo, branch)
        if diff:
            text = f"[dim]vs main:[/]\n{diff}"
        else:
            text = f"[dim]no differences vs main[/]"
        self.query_one("#branch-diff", Static).update(text)

    def _refresh(self) -> None:
        node = self._ssh.active.name
        # Status
        porcelain = self._ssh.remote_git_status(node, self._repo)
        if porcelain:
            lines = porcelain.splitlines()
            status_lines = [l for l in lines if not l.startswith("##")]
            branch_line = lines[0].replace("## ", "") if lines else ""
            status_text = f"Branch: {branch_line}\n"
            if status_lines:
                status_text += "\n".join(status_lines[:10])
            else:
                status_text += "Working tree clean"
        else:
            status_text = "[red]Not a git repo or connection failed[/]"
        self.query_one("#git-status", Static).update(status_text)
        # Log
        log = self._ssh.remote_git_log(node, self._repo, 10)
        self.query_one("#git-log", Static).update(log or "[dim]No commits[/]")
        # Branches
        branches = self._ssh.remote_git_branches(node, self._repo)
        current = self._ssh.remote_git_current_branch(node, self._repo)
        lv = self.query_one("#branch-list", ListView)
        lv.clear()
        for b in branches:
            marker = "[green]*[/] " if b == current else "  "
            item = ListItem(Label(f"{marker}{b}"))
            item.data = b
            lv.append(item)
        if not branches:
            lv.append(ListItem(Label("[dim]No branches[/]")))
        # Keep focus on the branch list after refresh.
        lv.focus()

    def action_fetch(self) -> None:
        self.query_one("#git-output", Static).update("[yellow]Fetching...[/]")
        node = self._ssh.active.name
        ok = self._ssh.remote_git_fetch(node, self._repo)
        if ok:
            self.query_one("#git-output", Static).update("[green]Fetch OK[/]")
            self._refresh()
        else:
            self.query_one("#git-output", Static).update("[red]Fetch failed[/]")

    def action_pull(self) -> None:
        self.query_one("#git-output", Static).update("[yellow]Pulling...[/]")
        node = self._ssh.active.name
        ok, msg = self._ssh.remote_git_pull(node, self._repo)
        if ok:
            self.query_one("#git-output", Static).update(f"[green]Pull OK[/]\n{msg}")
            self._refresh()
        else:
            self.query_one("#git-output", Static).update(f"[red]Pull failed[/]\n{msg}")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "branch-list":
            return
        branch = event.item.data
        if not branch:
            return
        node = self._ssh.active.name
        ok, msg = self._ssh.remote_git_checkout(node, self._repo, branch)
        if ok:
            self.query_one("#git-output", Static).update(f"[green]Checked out {branch}[/]")
            self._refresh()
        else:
            self.query_one("#git-output", Static).update(f"[red]Checkout failed: {branch}[/]\n{msg}")


def main() -> None:
    app = JupyterHubTUI()
    app.run()


if __name__ == "__main__":
    main()
