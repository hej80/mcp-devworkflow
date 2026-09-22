"""
Error taxonomy + safe error reporting.

BONUS BEST PRACTICE -- "Error Disclosure: avoid leaking sensitive information
in error messages".

Rule used throughout this server:
  * The *client* (Claude Desktop, and therefore the LLM and the end user) gets
    a short, actionable message plus a random `error_id`.
  * The full exception, stack trace, file paths and tokens stay in the server's
    stderr log, which only the operator can read.
  * An operator matches a user report to a log line via the `error_id`.

IMPORTANT (MCP stdio transport): logging MUST go to stderr. stdout carries the
JSON-RPC stream, so a single stray `print()` corrupts the protocol and the
server appears to "crash" in Claude Desktop.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Any

# Handler is bound to stderr on purpose -- see module docstring.
_handler = logging.StreamHandler(stream=sys.stderr)
_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
)
logging.basicConfig(level=logging.INFO, handlers=[_handler], force=True)

log = logging.getLogger("devworkflow")


class DevWorkflowError(Exception):
    """Base class for errors that are safe to show to the client verbatim."""

    code = "ERROR"


class ValidationError(DevWorkflowError):
    """Input failed sanitization. Safe to echo: it describes the rule, not the system."""

    code = "INVALID_INPUT"


class RateLimitError(DevWorkflowError):
    """Caller exceeded the configured budget for a tool."""

    code = "RATE_LIMITED"

    def __init__(self, message: str, retry_after_seconds: float) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AuthError(DevWorkflowError):
    """Missing/invalid credentials. Message never includes the credential."""

    code = "AUTH_ERROR"


class UpstreamError(DevWorkflowError):
    """GitHub or git returned a failure we can describe without leaking detail."""

    code = "UPSTREAM_ERROR"


def ok(**payload: Any) -> dict[str, Any]:
    """Uniform success envelope so the LLM always sees the same shape."""
    return {"ok": True, **payload}


def fail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """Uniform failure envelope."""
    return {"ok": False, "error": {"code": code, "message": message, **extra}}


def safe_failure(exc: Exception, *, tool: str, redact=lambda s: s) -> dict[str, Any]:
    """Convert any exception into a client-safe response.

    Known (DevWorkflowError) exceptions carry messages we wrote ourselves, so
    they are returned as-is. Anything else is an unexpected bug: the client is
    told only that something went wrong, plus an id to quote to the operator.
    """
    if isinstance(exc, RateLimitError):
        log.warning("tool=%s rate limited: %s", tool, exc)
        return fail(
            exc.code,
            str(exc),
            retry_after_seconds=round(exc.retry_after_seconds, 1),
        )

    if isinstance(exc, DevWorkflowError):
        log.info("tool=%s rejected: %s", tool, redact(str(exc)))
        return fail(exc.code, redact(str(exc)))

    error_id = uuid.uuid4().hex[:12]
    # Full detail -> operator-only log (stderr). exc_info gives the traceback.
    log.error("tool=%s unhandled error id=%s", tool, error_id, exc_info=exc)
    return fail(
        "INTERNAL_ERROR",
        "The tool failed because of an internal error. No details are exposed "
        f"by design. Quote error_id {error_id} to the server operator, who can "
        "find the full trace in the server log.",
        error_id=error_id,
    )
