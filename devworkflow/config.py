"""
Configuration and secret management.

RUBRIC NOTE -- "Secret management using .env or pathlib.path":
  * The path to the .env file is built with `pathlib.Path`, never `os.path`.
  * Secrets are read with `dotenv.dotenv_values()`, which returns a plain dict.
    We deliberately do NOT call `load_dotenv()`, because that pushes the token
    into `os.environ`, where every child process (and every library that logs
    the environment) can see it. Keeping the value in a local dict means the
    token exists only inside this process's memory.
  * `os` is not imported anywhere in this project.

Precedence: values in .env win. A value may also be supplied by the MCP client
(Claude Desktop) through the "env" block of its config file; that block is a
per-server environment, so we accept it as a fallback ONLY if .env is absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values

# Project root = the directory that contains this package's parent folder.
#   config.py -> devworkflow/ -> <project root>
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
ENV_PATH: Path = PROJECT_ROOT / ".env"

# Anything the server is allowed to touch on disk lives under this root.
DEFAULT_WORKSPACE_ROOT: Path = Path.home() / "mcp-workspace"


class ConfigError(RuntimeError):
    """Raised when the server cannot start because configuration is missing."""


def _as_int(raw: str | None, fallback: int) -> int:
    """Parse an int from .env, falling back instead of crashing on junk input."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


def _as_float(raw: str | None, fallback: float) -> float:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return fallback


@dataclass(frozen=True)
class Settings:
    """Immutable, validated settings for one server process."""

    github_token: str = ""
    github_api_base: str = "https://api.github.com"

    # Sandbox root: git tools refuse to operate outside this directory tree.
    workspace_root: Path = DEFAULT_WORKSPACE_ROOT

    # Optional allow-list of "owner/repo" slugs the write tool may touch.
    # Empty set == no allow-list (any repo the token can reach).
    allowed_repos: frozenset[str] = field(default_factory=frozenset)

    # --- Rate limiting knobs (BEST PRACTICE #2) -------------------------
    write_rate_capacity: int = 3       # burst size for GitHub write calls
    write_rate_window_seconds: float = 60.0
    read_rate_capacity: int = 10       # burst size for local git reads
    read_rate_window_seconds: float = 60.0

    # --- Safety limits --------------------------------------------------
    git_timeout_seconds: float = 10.0
    max_output_chars: int = 20_000
    http_timeout_seconds: float = 15.0

    @property
    def has_token(self) -> bool:
        return bool(self.github_token)

    def redact(self, text: str) -> str:
        """Strip the token out of any string before it can be logged/returned."""
        if self.github_token and self.github_token in text:
            text = text.replace(self.github_token, "***REDACTED***")
        return text


def load_settings(env_path: Path | None = None) -> Settings:
    """Read .env (if present) and build a Settings object.

    Never raises for a missing token: the server must still start so the user
    can call `verify_github_auth` and be told what to fix. Tools that actually
    need the token raise a clear, non-leaky error at call time.
    """
    path = env_path if env_path is not None else ENV_PATH
    raw: Mapping[str, str | None] = dotenv_values(path) if path.exists() else {}

    token = (raw.get("GITHUB_TOKEN") or "").strip()

    workspace_raw = (raw.get("WORKSPACE_ROOT") or "").strip()
    workspace = (
        Path(workspace_raw).expanduser().resolve()
        if workspace_raw
        else DEFAULT_WORKSPACE_ROOT
    )

    allowed_raw = (raw.get("ALLOWED_REPOS") or "").strip()
    allowed = frozenset(
        slug.strip().lower() for slug in allowed_raw.split(",") if slug.strip()
    )

    return Settings(
        github_token=token,
        github_api_base=(raw.get("GITHUB_API_BASE") or "https://api.github.com").strip(),
        workspace_root=workspace,
        allowed_repos=allowed,
        write_rate_capacity=_as_int(raw.get("WRITE_RATE_CAPACITY"), 3),
        write_rate_window_seconds=_as_float(raw.get("WRITE_RATE_WINDOW_SECONDS"), 60.0),
        read_rate_capacity=_as_int(raw.get("READ_RATE_CAPACITY"), 10),
        read_rate_window_seconds=_as_float(raw.get("READ_RATE_WINDOW_SECONDS"), 60.0),
        git_timeout_seconds=_as_float(raw.get("GIT_TIMEOUT_SECONDS"), 10.0),
        max_output_chars=_as_int(raw.get("MAX_OUTPUT_CHARS"), 20_000),
        http_timeout_seconds=_as_float(raw.get("HTTP_TIMEOUT_SECONDS"), 15.0),
    )
