"""
devworkflow -- a Model Context Protocol (MCP) server for Development Workflow tasks.

Package layout (each module has one job, so the server file stays thin):

    config.py      Loads settings/secrets from .env using pathlib + python-dotenv.
    errors.py      Error taxonomy + "safe error" wrapper (no information disclosure).
    security.py    BEST PRACTICE #1 -- input sanitization / validation.
    ratelimit.py   BEST PRACTICE #2 -- token-bucket rate limiting.
    git_ops.py     Thin, safe wrapper around the local `git` binary (no shell).
    github_api.py  Authenticated GitHub REST v3 client (httpx).

The MCP entry point lives in ../server.py.
"""

__version__ = "1.0.0"
