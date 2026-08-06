# jupyter-hub-tui

Textual TUI for managing remote cluster access. Replaces JupyterHub for SSH,
git, venv, and notebook workflows. All operations happen over SSH.

## What it does

- SSH into cluster nodes from an embedded terminal
- Browse remote files and open `.ipynb` notebooks in tabs
- Run notebook cells on a remote IPython kernel
- Display plot output as full-resolution images in the terminal (kitty)
- Remote git status, log, branches, fetch, pull, checkout
- Proxy jump support for nodes behind a login node
- SSH ControlMaster so all commands reuse one connection

## Setup

1. Clone the repo.
2. `python3 -m venv .venv && source .venv/bin/activate`
3. `pip install -r requirements.txt`
4. Copy `config.example.json` to `config.json`.
5. Fill in your cluster node details.
6. Run: `python3 -m jupyter_hub_tui`

Run inside **kitty** for inline image rendering. Other terminals will work
for everything except images.

## How notebooks work

No euporie, no browser, no JupyterHub. The TUI talks to the kernel directly.

1. Open a `.ipynb` file from the remote file browser.
2. The file is read over SSH (`cat`) and parsed with `nbformat`.
3. A remote IPython kernel starts on the cluster node via SSH.
4. The kernel prints its connection file path. The TUI reads it over SSH.
5. The TUI opens SSH port tunnels for all five ZMQ ports (shell, iopub,
   stdin, control, heartbeat) on a single SSH connection.
6. The TUI connects to the kernel locally via `jupyter_client`.
7. Run a cell with `Ctrl+E`. Code goes out over ZMQ, output comes back.
8. Matplotlib output is forced to `Agg` backend so plots render to PNG
   buffers. PNG data comes back as base64 over iopub.
9. The TUI decodes the PNG and renders it via `textual-image` TGPImage,
   which uses the kitty graphics protocol to display full-resolution
   images inline.

### Architecture

```
jupyter_hub_tui/
  app.py            Main TUI. Tabs, sidebar, keybindings, screens.
  terminal.py       Embedded PTY terminal (custom VT100 parser). SSH shell lives here.
  notebook_view.py  Notebook renderer. Cell editors, output, images.
  kernel_client.py  Remote IPython kernel manager. SSH tunnels + ZMQ.
  ssh_manager.py    SSH command builder. ControlMaster, proxy jumps.
  git_status.py     Remote git porcelain parser.
  config.py         config.json loader.
  apc.py            APC sequence parser (unused after euporie removal).
```

### Remote requirements

The cluster node must have:
- Python with `ipykernel` installed in the venv
- The venv activation command must put ipykernel on PATH
- SSH access (password or key)

The `activate_cmd` config key tells the TUI how to activate the venv on
the remote. Example: `source ~/.bashrc && icetop-cnn`.

The `pythonpath` config key sets `PYTHONPATH` before the kernel starts.
This matters when your repo has modules that shadow installed packages
(e.g. a local `utils.py` shadowing PyPI `utils`).

## Controls

### Global

| Key | Action |
|-----|--------|
| `1`-`3` | Connect to node by index (dashboard) |
| `Tab` | Toggle focus between panels (dashboard) |
| `Ctrl+\` | Toggle sidebar (works during SSH) |
| `Ctrl+N` | Disconnect and return to dashboard |
| `Ctrl+E` | Edit active node (host, user, port, proxy) |
| `Ctrl+M` | View cluster manual |
| `Ctrl+K` | Generate and copy SSH keys to all nodes |
| `Ctrl+R` | Refresh status and file tree |
| `Ctrl+G` | Pick git repo path (browse remote dirs) |
| `Ctrl+B` | Git screen: status, log, branches |
| `Ctrl+H` | Show help |
| `Esc` | Quit |

### Terminal (SSH session)

| Key | Action |
|-----|--------|
| `Ctrl+\` | Toggle file browser sidebar |
| `Ctrl+W` | Close current tab |
| `Ctrl+Left/Right` | Switch tabs |

### Notebook tabs

| Key | Action |
|-----|--------|
| `Ctrl+E` | Run current cell |
| `Ctrl+R` | Run cell, move to next |
| `Ctrl+S` | Save notebook to remote |
| `Ctrl+I` | Interrupt kernel |
| `Ctrl+W` | Close tab (shuts down kernel) |

### Git screen (Ctrl+B)

| Key | Action |
|-----|--------|
| `f` | Fetch |
| `p` | Pull |
| `Enter` | Checkout selected branch |
| `Esc` | Close |

## Config

`config.json` is gitignored. `config.example.json` is the template.

### Nodes

```json
{
  "nodes": {
    "login": {
      "host": "login.example.org",
      "user": "youruser",
      "port": 22,
      "description": "Primary login node"
    },
    "worker-1": {
      "host": "worker-1.example.org",
      "user": "youruser",
      "port": 22,
      "description": "Compute node",
      "proxy": "login"
    }
  }
}
```

Nodes behind a login node use the `proxy` field. The value must match
another node name. SSH uses `-J user@host:port` for the hop.

### venv

```json
{
  "venv": {
    "activate_cmd": "source ~/.bashrc && icetop-cnn",
    "pythonpath": "/home/youruser/icetop-cnn"
  }
}
```

`activate_cmd`: Shell command that activates the remote venv. Must put
`ipykernel` on PATH so the TUI can start a kernel.

`pythonpath`: Optional. Prepended as `PYTHONPATH` before kernel start.

### git

```json
{
  "git": {
    "repo_path": "/home/youruser/icetop-cnn"
  }
}
```

Set via `Ctrl+G` (browses the remote filesystem). Git status, log,
branches, fetch, pull, and checkout all operate on the remote repo.

### browse_path

Starting directory for the remote file browser. Defaults to `~`.

## ControlMaster

The interactive SSH session becomes a ControlMaster. All remote commands
(file browser, git, kernel, file read/write) reuse that connection without
re-authenticating. One passphrase entry per session.

## Self-checks

Each module has a `__main__` self-check:

```
python3 -m jupyter_hub_tui.ssh_manager
python3 -m jupyter_hub_tui.kernel_client
python3 -m jupyter_hub_tui.notebook_view
python3 -m jupyter_hub_tui.git_status
```
