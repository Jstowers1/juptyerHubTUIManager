# jupyter-hub-tui

A terminal UI that runs remote Jupyter notebooks over SSH. No browser, no web server, no port forwarding setup. One binary, one SSH connection, full notebooks in the terminal.

## Highlights

- **Full notebooks in the terminal.** Open any remote `.ipynb`, edit cells, execute code, and read output without leaving the TUI.
- **Inline plot rendering.** Matplotlib figures display as full-resolution images inside the terminal through the kitty graphics protocol. No manual sizing, no external viewer.
- **One SSH connection for everything.** A single ControlMaster session multiplexes the shell, file browser, git, file transfers, and kernel traffic. One passphrase entry per session.
- **Custom VT100 terminal emulator.** The embedded terminal parses escape sequences in pure Python. It coexists with the TUI widget tree in one process.
- **Kernel startup without blocking.** Cells render and accept edits while the kernel boots. Run requests queue and execute in order when the kernel is ready.
- **Remote kernel selection.** Pick any Jupyter kernelspec on the remote host. The client resolves its launch command, environment, and argv automatically.
- **Syntax-colored editing.** Tree-sitter highlights code in display and edit modes. Kernel language names are normalized automatically.

## Tech Stack

| Layer | Technology |
|-------|------------|
| TUI framework | Textual |
| Terminal emulation | Custom VT100 parser |
| Kernel protocol | ZMQ via jupyter_client |
| Notebook format | nbformat |
| Image rendering | kitty graphics protocol via textual-image |
| Syntax highlighting | tree-sitter |
| Image decoding | Pillow |
| Transport | OpenSSH with ControlMaster |

## Install

Requires Python 3.11+ and OpenSSH.

```
pipx install git+https://github.com/Jstowers1/juptyerHubTUIManager.git
```

Or from a checkout:

```
git clone https://github.com/Jstowers1/juptyerHubTUIManager.git
cd juptyerHubTUIManager
pip install .

For a pipx install, create the config once:

```
jhtui --init-config
```

This writes `~/.config/jhtui/config.json` from the template. Edit it with your node list.

Or install the latest release with pipx:

```
pipx install git+https://github.com/Jstowers1/juptyerHubTUIManager.git@v<latest-tag>
```

Every `v*` git tag now builds a wheel and publishes a GitHub release automatically.

This installs the `jhtui` binary. The binary starts `ssh-agent`, loads the default key, then runs the TUI. Enter the key passphrase once per session.

Run inside kitty for inline images. Every other feature works in any terminal.

## Update

Install the new release wheel over the old one:

```
pipx install --force https://github.com/Jstowers1/juptyerHubTUIManager/releases/download/v1.3.0/jupyter_hub_tui-1.3.0-py3-none-any.whl
```

Swap `v1.3.0` for the latest tag. Find the wheel URL on each release page.

Check the installed version:

```
pipx list
```

## Quick start

1. Copy the config template:

```
mkdir -p ~/.config/jhtui
cp config.example.json ~/.config/jhtui/config.json
```

2. Fill in node details. Each node needs a host, user, and port. Nodes behind a login node set `"proxy"` to the login node name.

3. Run `jhtui`.

4. Connect to a node from the list. The embedded terminal opens an SSH session. Press `Ctrl+\` to toggle the file sidebar.

5. Open a `.ipynb` file. It opens in a notebook tab with a kernel starting in the background.

## How it works

The TUI speaks the native Jupyter kernel protocol. It does not wrap a web frontend.

1. The user opens a notebook from the remote file browser. The TUI reads the file over SSH and renders the cells.
2. The TUI launches a detached kernel on the remote host through SSH. The launch waits until the kernel writes its connection file.
3. The TUI forwards all five ZMQ ports (shell, iopub, stdin, control, heartbeat) through the existing SSH master connection.
4. A local `jupyter_client` connects through the tunnels.
5. Cell execution sends code over ZMQ and collects output, images, and errors from iopub.
6. Matplotlib output is forced to the Agg backend. Plots arrive as PNG data and render inline at full resolution.

### Components

```
jupyter_hub_tui/
  app.py            Main TUI. Tabs, sidebar, keybindings, screens.
  terminal.py       Embedded PTY terminal with a custom VT100 parser.
  notebook_view.py  Notebook renderer. Cell editors, output, images.
  kernel_client.py  Remote kernel manager. Launch, tunnels, execution.
  ssh_manager.py    SSH command builder. ControlMaster, proxy jumps.
  git_status.py     Remote git porcelain parser.
  config.py         Config loader. User and repo locations.
  launcher.py       Binary entry point. ssh-agent setup.
```

## Controls

### Global

| Key | Action |
|-----|--------|
| `1`-`3` | Connect to node by index |
| `Tab` | Cycle sidebar widgets |
| `Ctrl+\` | Toggle sidebar |
| `Ctrl+T` | Toggle terminal focus |
| `Ctrl+N` | Disconnect |
| `Ctrl+H` | Help |
| `Esc` | Quit with confirmation |

### Notebooks

| Key | Action |
|-----|--------|
| `Enter` | Open notebook |
| `Ctrl+E` | Run cell |
| `Ctrl+R` | Run cell, move to next |
| `Ctrl+S` | Save notebook to remote |
| `Ctrl+I` | Interrupt kernel |
| `Ctrl+K` / `Ctrl+J` | Previous / next cell |
| `Ctrl+Shift+K` | Pick kernel spec |
| `Ctrl+W` | Close tab |

The focused cell is the editor. Type to edit. Moving cells commits changes.

### Git

| Key | Action |
|-----|--------|
| `Ctrl+G` | Pick repo path |
| `Ctrl+B` | Git screen |
| `f` | Fetch |
| `p` | Pull |
| `Enter` | Checkout branch |
| `Esc` | Close |

### Downloads

Right-click a file, folder, or cell image. Files and folders download to `./downloads/`. Folders download recursively. Cell images save as PNG.

## Configuration

The config file lives at `~/.config/jhtui/config.json`. A `config.json` next to the package takes priority in a dev checkout.

### Nodes

```json
{
  "nodes": {
    "login": {
      "host": "login.example.org",
      "user": "youruser",
      "port": 22
    },
    "worker-1": {
      "host": "worker-1.example.org",
      "user": "youruser",
      "port": 22,
      "proxy": "login"
    }
  }
}
```

The `proxy` field routes SSH through another node with a proxy jump.

### venv

```json
{
  "venv": {
    "activate_cmd": "source ~/.venv/bin/activate",
    "pythonpath": "/home/youruser/project"
  }
}
```

`activate_cmd` activates the remote environment before the kernel starts. `pythonpath` prepends `PYTHONPATH` for import shadowing.

## Remote requirements

- Python 3.11+ with `ipykernel` in the target environment
- `jupyter_client` available for kernelspec resolution
- OpenSSH server

## Self-checks

Each module runs a self-check:

```
python3 -m jupyter_hub_tui.ssh_manager
python3 -m jupyter_hub_tui.kernel_client
python3 -m jupyter_hub_tui.notebook_view
python3 -m jupyter_hub_tui.git_status
```

## License

MIT
