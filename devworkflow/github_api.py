"""
Authenticated GitHub REST v3 client (httpx).

AUTHENTICATION
--------------
A GitHub fine-grained Personal Access Token is read from `.env` by config.py
and sent as `Authorization: Bearer <token>`. The token:
  * is never written to a log, a tool result, or an error message
    (`Settings.redact()` scrubs it from any string on the way out);
  * is never placed in `os.environ`, so `git` subprocesses cannot read it;
  * is never put in a URL query string, where it would end up in access logs.

Request/response headers follow the documented GitHub API conventions:
https://docs.github.com/en/rest/issues/issues#create-an-issue
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings
from .errors import AuthError, UpstreamError, ValidationError

USER_AGENT = "devworkflow-mcp/1.0 (course project)"
API_VERSION = "2022-11-28"


class GitHubClient:
    """Thin synchronous wrapper. One instance per server process."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # -- internals --------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        if not self._settings.has_token:
            raise AuthError(
                "No GitHub token is configured. Create a .env file next to "
                "server.py containing GITHUB_TOKEN=<your fine-grained PAT>, "
                "then restart the server."
            )
        return {
            "Authorization": f"Bearer {self._settings.github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = f"{self._settings.github_api_base.rstrip('/')}{path}"
        try:
            with httpx.Client(timeout=self._settings.http_timeout_seconds) as client:
                return client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.TimeoutException:
            raise UpstreamError(
                f"GitHub did not respond within "
                f"{self._settings.http_timeout_seconds:.0f}s."
            ) from None
        except httpx.HTTPError as exc:
            # str(exc) can contain the URL but never the Authorization header;
            # redact() is belt-and-braces.
            raise UpstreamError(
                "Could not reach GitHub: "
                f"{self._settings.redact(type(exc).__name__)}."
            ) from None

    def _check(self, response: httpx.Response, *, context: str) -> None:
        """Map an HTTP status onto a safe, actionable error.

        Deliberately vague where being precise would leak: a 404 does not say
        whether the repository exists, because that is how private-repository
        existence gets enumerated.
        """
        status = response.status_code
        if status < 400:
            return
        if status in (401,):
            raise AuthError(
                "GitHub rejected the token (401). It is missing, expired, or "
                "revoked. Generate a new token and update .env."
            )
        if status == 403:
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                raise UpstreamError(
                    "GitHub's own API rate limit is exhausted for this token. "
                    "Wait for the quota window to reset."
                )
            raise AuthError(
                f"GitHub refused the request (403) while trying to {context}. "
                "The token most likely lacks the required permission "
                "(fine-grained tokens need Issues: Read and write)."
            )
        if status == 404:
            raise UpstreamError(
                f"GitHub returned 404 while trying to {context}. Either the "
                "repository does not exist or this token cannot see it."
            )
        if status == 422:
            raise ValidationError(
                f"GitHub rejected the data (422) while trying to {context}. "
                "Check that the repository accepts issues and that any labels "
                "already exist."
            )
        if status == 429:
            raise UpstreamError(
                "GitHub is throttling this token (429). Retry later."
            )
        raise UpstreamError(
            f"GitHub returned HTTP {status} while trying to {context}."
        )

    # -- public API -------------------------------------------------------
    def verify_auth(self) -> dict[str, Any]:
        """GET /user -- confirms the token works, without revealing the token."""
        response = self._request("GET", "/user")
        self._check(response, context="verify the token")
        data = response.json()
        return {
            "authenticated_as": data.get("login"),
            "account_type": data.get("type"),
            "api_calls_remaining": response.headers.get("x-ratelimit-remaining"),
        }

    def get_repo(self, slug: str) -> dict[str, Any]:
        """GET /repos/{owner}/{repo} -- metadata for the read-only resource."""
        response = self._request("GET", f"/repos/{slug}")
        self._check(response, context=f"read repository '{slug}'")
        data = response.json()
        return {
            "full_name": data.get("full_name"),
            "description": data.get("description"),
            "default_branch": data.get("default_branch"),
            "private": data.get("private"),
            "language": data.get("language"),
            "open_issues": data.get("open_issues_count"),
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks_count"),
            "updated_at": data.get("updated_at"),
            "html_url": data.get("html_url"),
        }

    def create_issue(
        self,
        slug: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /repos/{owner}/{repo}/issues."""
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        response = self._request("POST", f"/repos/{slug}/issues", json=payload)
        self._check(response, context=f"create an issue in '{slug}'")
        data = response.json()
        return {
            "issue_number": data.get("number"),
            "url": data.get("html_url"),
            "state": data.get("state"),
            "created_at": data.get("created_at"),
        }
