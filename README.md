# jupyter-hub-tui

TUI for managing IceTop research cluster access.
Replaces JupyterHub dependency for SSH, git, venv, and notebook workflows.

## Setup

1. Copy `config.example.json` to `config.json`.
2. Fill in your cluster node details.
3. Install dependencies: `pip install textual`
4. Run: `python -m jupyter_hub_tui`

## Config

`config.json` holds node connection details, venv path, and Jupyter settings.
This file is gitignored. `config.example.json` is the template for peers.

## Features

- SSH connection manager with quick-switch between nodes
- Venv activation status indicator
- Git status overview
- Offline cluster manual rendered in-app
- Jupyter notebook launch with browser handoff
