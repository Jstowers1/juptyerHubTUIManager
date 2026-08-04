# jupyter-hub-tui

TUI for managing remote cluster access.
Replaces JupyterHub dependency for SSH, git, venv, and notebook workflows.

## Setup

1. Clone the repo.
2. Create a venv: `python3 -m venv .venv && source .venv/bin/activate`
3. Install dependencies: `pip install -r requirements.txt`
4. Copy `config.example.json` to `config.json`.
5. Fill in your cluster node details.
6. Run: `python -m jupyter_hub_tui`

## Controls

| Key | Action |
|-----|--------|
| `1`-`3` | Connect to node by index (dashboard only) |
| `Tab` | Toggle focus between panels (dashboard) |
| `Ctrl+\` | Toggle sidebar (works during SSH) |
| `Ctrl+N` | Disconnect and return to dashboard |
| `Ctrl+E` | Edit active node details (host, user, port, proxy) |
| `Ctrl+M` | View cluster manual |
| `Ctrl+J` | Launch Jupyter on active node (SSH tunnel + euporie in PTY) |
| `Ctrl+K` | Generate and copy SSH keys to all nodes |
| `Ctrl+R` | Refresh status bar and file tree |
| `Ctrl+G` | Pick git repo path on remote (browse remote dirs) |
| `Ctrl+B` | Git screen: status, log, branches, fetch, pull, checkout |
| `Ctrl+H` | Show help |
| `Esc` | Quit |

During SSH, all Ctrl keybindings work via terminal escape hatches.
The terminal intercepts them before sending to the PTY.

### Inside the Git screen (Ctrl+B)

| Key | Action |
|-----|--------|
| `f` | Fetch |
| `p` | Pull |
| `Enter` | Checkout selected branch |
| `Esc` | Close |

## Status bar

The bottom bar shows venv state, active node, and git branch/dirty status:

```
 VENV:ON  CONNECTED:worker-1  git:main*
```

## Config

`config.json` holds node connection details, venv paths (local and remote),
git repo path, and Jupyter settings. This file is gitignored.
`config.example.json` is the template.

### Proxy jumps

Nodes behind a login node use the `proxy` field. The value must match
another node name in the config. SSH uses `-J user@host:port` for the hop.

Example: worker-1 and worker-2 are behind login:

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
    },
    "worker-2": {
      "host": "worker-2.example.org",
      "user": "youruser",
      "port": 22,
      "description": "Compute node",
      "proxy": "login"
    }
  }
}
```

### Git repo path

Set via Ctrl+G (browses the remote filesystem). Git status, log, branches,
fetch, pull, and checkout all operate on the remote repo.

### ControlMaster

The interactive SSH session becomes a ControlMaster. All remote commands
(file browser, git, branches) reuse that connection without re-authenticating.

## Notes

- SSH sessions run in an embedded terminal in the right panel.
  Password and passphrase prompts work inline.
- Run inside kitty for inline notebook graphics via euporie.
- Notebooks live on the remote servers. Ctrl+J SSH-tunnels the Jupyter
  port and launches euporie inside the PTY to render them.
