"""
A deliberately small, safe wrapper around the local `git` binary.

Security properties (see security.py for the threat model):
  * `shell=False` (the default) and an argv LIST -- never a string. This is the
    single most important line in the file: with a list, `;` `|` `$()` are just
    characters in an argument, because no shell ever sees them.
  * Every user-supplied value is glued to its flag as ONE token
    (`--author=<value>`), so it cannot be re-read as a separate option.
  * A hard timeout, so a hung `git` cannot wedge the MCP server.
  * A minimal environment: the parent process environment (which may hold
    credentials) is NOT inherited, and `GIT_TERMINAL_PROMPT=0` stops git from
    blocking on an interactive credential prompt.
  * Output is truncated, bounding memory and the client's context window.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import UpstreamError, ValidationError

# Field separator for `git log --pretty`: 0x1F (ASCII UNIT SEPARATOR) can never
# appear in a commit subject, so parsing is unambiguous. Splitting on ',' or
# '|' would break on the first commit message containing one.
FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"


def _git_binary() -> str:
    """Locate git once, by absolute path, instead of trusting a PATH lookup."""
    found = shutil.which("git")
    if not found:
        raise UpstreamError(
            "git is not installed or not on this server's PATH, so local "
            "repository tools are unavailable."
        )
    return found


def _minimal_env(git_path: str) -> dict[str, str]:
    """Build the child environment explicitly rather than inheriting it."""
    return {
        # Only the directory holding git itself; nothing else is needed.
        "PATH": str(Path(git_path).parent),
        "HOME": str(Path.home()),
        "GIT_TERMINAL_PROMPT": "0",   # never block waiting for a password
        "GIT_CONFIG_NOSYSTEM": "1",   # ignore /etc/gitconfig
        "LC_ALL": "C",                # stable, parseable output
    }


@dataclass(frozen=True)
class Commit:
    sha: str
    author_name: str
    author_email: str
    date: str
    subject: str

    def as_dict(self) -> dict[str, str]:
        return {
            "sha": self.sha[:10],
            "author": self.author_name,
            "email": self.author_email,
            "date": self.date,
            "subject": self.subject,
        }


def run_git(
    args: list[str],
    *,
    cwd: Path,
    timeout: float = 10.0,
    max_chars: int = 20_000,
) -> str:
    """Run `git <args>` inside `cwd` and return stdout.

    `args` must already be validated by security.py. Raises UpstreamError on a
    non-zero exit, with git's own stderr trimmed to one short line so that a
    failure cannot dump filesystem layout into the model's context.
    """
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        # Guards against a future caller passing a single string, which would
        # be the classic way this file could become injectable again.
        raise ValidationError("internal: git arguments must be a list of strings.")

    git = _git_binary()
    try:
        completed = subprocess.run(   # noqa: S603 - shell=False by default
            [git, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=_minimal_env(git),
            check=False,
            shell=False,              # stated explicitly for the reader
        )
    except subprocess.TimeoutExpired:
        raise UpstreamError(
            f"git did not finish within {timeout:.0f}s and was terminated."
        ) from None

    if completed.returncode != 0:
        first_line = (completed.stderr or "").strip().splitlines()
        detail = first_line[0][:200] if first_line else "no details available"
        raise UpstreamError(f"git exited with status {completed.returncode}: {detail}")

    out = completed.stdout
    if len(out) > max_chars:
        out = out[:max_chars] + f"\n... [truncated at {max_chars} characters]"
    return out


def recent_commits(
    repo: Path,
    *,
    author: str = "",
    max_count: int = 20,
    timeout: float = 10.0,
    max_chars: int = 20_000,
) -> list[Commit]:
    """Return recent commits, optionally filtered by author."""
    pretty = FIELD_SEP.join(["%H", "%an", "%ae", "%aI", "%s"]) + RECORD_SEP
    args = [
        "--no-pager",
        "log",
        f"--max-count={max_count}",   # one token: value cannot become a flag
        f"--pretty=format:{pretty}",
        "--no-color",
    ]
    if author:
        args.append(f"--author={author}")  # one token, see module docstring

    raw = run_git(args, cwd=repo, timeout=timeout, max_chars=max_chars)

    commits: list[Commit] = []
    for record in raw.split(RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        parts = record.split(FIELD_SEP)
        if len(parts) != 5:
            continue  # ignore the truncation marker or a malformed tail
        sha, name, email, date, subject = parts
        commits.append(Commit(sha, name, email, date, subject))
    return commits


def repo_status(repo: Path, *, timeout: float = 10.0) -> dict[str, object]:
    """Current branch, dirty-file count and total commit count."""
    branch = run_git(
        ["--no-pager", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, timeout=timeout
    ).strip()
    porcelain = run_git(
        ["--no-pager", "status", "--porcelain"], cwd=repo, timeout=timeout
    )
    dirty = [line for line in porcelain.splitlines() if line.strip()]
    total = run_git(
        ["--no-pager", "rev-list", "--count", "HEAD"], cwd=repo, timeout=timeout
    ).strip()
    return {
        "branch": branch,
        "uncommitted_changes": len(dirty),
        "total_commits": int(total) if total.isdigit() else None,
    }
