# Cluster Manual

This manual replaces the stale wiki. Update it when procedures change.

## Connecting

| Node | Purpose | Command |
|------|---------|---------|
| pub | General login | `ssh user@pub` |
| cobalt | IceTop tools | `ssh -J user@pub user@cobalt` |
| npx-submitter | HTC job submission | `ssh -J user@pub user@npx-submitter` |

Use `kitty +kitten ssh` instead of `ssh` for proper terminfo and graphics support.

## Venv

Always activate before running IceTop tools:

```bash
source ~/.venv/icetop/bin/activate
```

The TUI status bar shows VENV:ON when active.

## Submitting HTC Jobs

1. SSH to npx-submitter (via pub proxy jump).
2. Activate venv.
3. Submit with `condor_submit`.

## Git

No dedicated git server on cluster. Use a local clone and rsync.
