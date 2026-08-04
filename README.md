# jupyter-hub-tui

TUI for managing IceTop research cluster access.
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
| `1`-`9` | Connect to node by index |
| `Ctrl+N` | Disconnect and return to dashboard |
| `Ctrl+E` | Edit active node details (host, user, port, proxy) |
| `Ctrl+M` | View cluster manual |
| `Ctrl+J` | Launch Jupyter on active node (SSH tunnel + euporie) |
| `Ctrl+K` | Generate and copy SSH keys to all nodes |
| `Ctrl+R` | Refresh status bar and file tree |
| `Esc` | Quit |

Node selection also works by arrow-navigating the list and pressing Enter.

## Status bar

The bottom bar shows venv state, active node, and git branch/dirty status:

```
 VENV:ON  CONNECTED:cobalt  git:main*
```

## Config

`config.json` holds node connection details, venv paths (local and remote),
git repo path, and Jupyter settings. This file is gitignored.
`config.example.json` is the template for peers.

Node configs support a `proxy` field for jump hosts. Cobalt and npx-submitter
route through pub automatically via SSH `ProxyJump`.

## Notes

- SSH sessions run in an embedded terminal in the right panel. No tmux or
  separate windows needed. Password prompts work inline.
- Run inside kitty for inline notebook graphics via euporie.
- Press `k` to generate an ed25519 key (if none exists) and copy it to all
  configured nodes via `ssh-copy-id`.
