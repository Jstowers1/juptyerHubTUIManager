# Git status via subprocess. No hardcoding, uses cwd of the TUI.

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitStatus:
    branch: str
    dirty: bool
    ahead: int
    behind: int
    staged: int
    modified: int
    untracked: int


def status(path: str = ".") -> GitStatus | None:
    # Get git status for a repo. Returns None if not a git repo.
    branch = _branch(path)
    if not branch:
        return None
    porcelain = _run_git(path, ["status", "--porcelain=v1", "-b"])
    staged = modified = untracked = 0
    if porcelain:
        for line in porcelain.splitlines():
            if line.startswith("??"):
                untracked += 1
            elif line.startswith("  "):
                modified += 1
            else:
                staged += 1
    ahead, behind = _parse_ahead_behind(porcelain)
    return GitStatus(
        branch=branch,
        dirty=bool(staged + modified + untracked),
        ahead=ahead,
        behind=behind,
        staged=staged,
        modified=modified,
        untracked=untracked,
    )


def _branch(path: str) -> str | None:
    rev_parse = _run_git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if rev_parse is None:
        return None
    return rev_parse.strip()


def _parse_ahead_behind(porcelain: str | None) -> tuple[int, int]:
    if not porcelain:
        return (0, 0)
    first_line = porcelain.splitlines()[0]
    ahead = behind = 0
    if "ahead" in first_line:
        ahead = int(first_line.split("ahead ")[1].split(",")[0].strip())
    if "behind" in first_line:
        behind = int(first_line.split("behind ")[1].split(",")[0].strip())
    return (ahead, behind)


def _run_git(path: str, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", path, *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _self_check() -> None:
    # Self-check: run against this repo.
    repo = str(Path(__file__).resolve().parent.parent)
    s = status(repo)
    assert s is not None, "Not a git repo?"
    assert s.branch == "main", f"Expected main, got {s.branch}"
    print(f"Git self-check passed: branch={s.branch} dirty={s.dirty}")


if __name__ == "__main__":
    _self_check()
