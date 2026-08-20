# Cluster Manual

## Nodes

| Node | Purpose | Access |
|------|---------|--------|
| login | Primary login | `ssh user@login` |
| worker-1 | Compute | `ssh -J user@login user@worker-1` |
| worker-2 | Compute | `ssh -J user@login user@worker-2` |

## Environment

Activate before running tools:

```bash
source ~/.venv/bin/activate
```

## Submitting jobs

1. SSH to the submit node (via login proxy jump).
2. Use the cluster scheduler to submit your job script.
3. Monitor with the scheduler status command.

## Git

The tracked repo lives on the remote system. Use Ctrl+G to set the path,
Ctrl+B to view branches. Inside the git screen: f fetches, p pulls,
Enter or c checks out the selected branch. The current branch is marked
with a green *.

## Downloads

Right-click a file or folder in the sidebar tree to download it to the
local ./downloads/ directory. Folders download recursively. Right-click
a cell image to save the PNG.
