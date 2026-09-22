# devworkflow — an MCP server for Development Workflow

**Author: Hamdan Emad Jabr Jaarour** · GitHub: hej80

A local [Model Context Protocol](https://modelcontextprotocol.io/) server that lets Claude Desktop
read a local git repository and file GitHub issues, with **input sanitization** and **rate limiting**
enforced on every call. It exposes 3 tools, 1 resource and 2 prompts over stdio (JSON-RPC), and
authenticates with a fine-grained GitHub token loaded from `.env`.

---

## 1. What the Server Exposes

### Tools

| Tool | What it does | Network? | Budget |
|---|---|---|---|
| `summarize_recent_commits(repo_path, author, max_count)` | Reads `git log` in a local repo and returns structured commits plus a per-author tally | No | 10 / 60 s |
| `create_github_issue(repo, title, body, labels, dry_run)` | Creates a GitHub issue over the authenticated REST API. `dry_run=true` returns a preview and sends nothing | Yes (write) | 3 / 30 s |
| `server_diagnostics(check_github)` | Reports config health, whether GitHub accepts the token, and live rate-limit budget. Never reveals the token | Optional | 20 / 60 s |

### Resource

```
repo://{owner}/{name}/summary
```

Read-only GitHub repository metadata as Markdown — description, default branch, visibility,
language, open issues, stars, last update. Example: `repo://modelcontextprotocol/python-sdk/summary`.

### Prompts

| Prompt | Purpose |
|---|---|
| `weekly_standup(repo_path, author, days)` | Turns commit history into a Shipped / In progress / Risks / Next step note |
| `triage_bug_report(raw_report, repo)` | Turns a messy bug report into a structured issue, previews it with `dry_run=true`, and requires confirmation before filing |

---

## 2. How a Request Flows

Every tool call passes through four stages, in this order:

1. **Rate limit** (`ratelimit.py`) — token bucket, one budget per tool
2. **Validate** (`security.py`) — allow-list charsets, no leading `-`, path confined to `WORKSPACE_ROOT`
3. **Execute** — `git_ops.py` (subprocess, `shell=False`) or `github_api.py` (httpx + Bearer token)
4. **Reply** (`errors.py`) — uniform `{ok, error:{code, message}}` envelope, no tracebacks or secrets

**The order is deliberate.** Budget is spent *before* validation, so a runaway loop cannot use the
tool as an unlimited validation oracle. Validation runs before any subprocess or HTTP call, so no
attacker-influenced value reaches `git` or `httpx`.

---

## 3. Setup

**Prerequisites:** Python 3.10+, `git` on your PATH, [Claude Desktop](https://claude.ai/download),
and [uv](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

### 3.1 Clone and Create the Virtual Environment

```bash
git clone https://github.com/hej80/mcp-devworkflow.git
cd mcp-devworkflow

uv venv                         # creates .venv/
source .venv/bin/activate       # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt
```

<details>
<summary>Without uv (stdlib venv)</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
</details>

In VS Code: **Cmd/Ctrl+Shift+P → Python: Select Interpreter → `./.venv/bin/python`**.

### 3.2 Create a GitHub Token

At [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new):
choose **Only select repositories** and pick one, set **Issues: Read and write**, set a short expiry,
then generate. GitHub shows the token once.

Fine-grained over classic on purpose: a classic token with `repo` scope can read and write **every**
repository you own; this one touches one repo, and only its issues.

### 3.3 Configure `.env`

```bash
cp .env.example .env
```

```ini
GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx
WORKSPACE_ROOT=/Users/hamdanjaarour/mcp-workspace
ALLOWED_REPOS=hej80/mcp-test
```

```bash
mkdir -p ~/mcp-workspace
cd ~/mcp-workspace && git clone https://github.com/hej80/mcp-test.git
```

`WORKSPACE_ROOT` is a sandbox: the git tool refuses any path resolving outside it. `.env` is
gitignored and must never be committed.

### 3.4 Verify

```bash
python tests/test_security_and_ratelimit.py   # 14 unit tests
python tests/smoke_test_stdio.py              # starts the server, speaks MCP to it
```

`smoke_test_stdio.py` launches `server.py` and talks JSON-RPC over stdio exactly as Claude Desktop
does. If it prints **PASS**, Claude Desktop can start the server.

---

## 4. Connect to Claude Desktop

Config file — or use **Settings → Developer → Edit Config**:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

Paste [`claude_desktop_config.json`](claude_desktop_config.json) from this repo, replacing both
paths with your own **absolute** ones:

```json
{
  "mcpServers": {
    "devworkflow": {
      "command": "/Users/hamdanjaarour/mcp-devworkflow/.venv/bin/python",
      "args": ["/Users/hamdanjaarour/mcp-devworkflow/server.py"]
    }
  }
}
```

A uv-based alternative is in [`claude_desktop_config.uv.json`](claude_desktop_config.uv.json).

Three things that break this for most people:

- **Paths must be absolute.** Claude Desktop does not run your shell, so `python`, `~` and relative
  paths do not resolve.
- **Use the venv's interpreter**, not the system one, or the `mcp` package is not found.
- **No secrets in this file.** It is world-readable plaintext; the token stays in `.env`. Committing
  it with an `env` block containing a token is how these tokens leak.

Then **quit Claude Desktop fully** (Cmd+Q) and reopen. Tools appear under the search-and-tools icon,
prompts under **+**. Try: *"Run server_diagnostics and tell me if my GitHub token works."*

---

## 5. The Two Best Practices

```bash
python exploits/exploit_injection.py    # ~2 seconds
python exploits/exploit_rate_limit.py   # ~11 seconds
```

### Best Practice #1 — Input Sanitization (`devworkflow/security.py`)

Tool arguments are written by an LLM whose context may contain attacker-controlled text — a README,
an issue body, a pasted log. They are untrusted input, exactly like an HTTP form field.

**Part A** attacks deliberately vulnerable code that exists only inside the exploit file:

| Attack | Vulnerable implementation | Result |
|---|---|---|
| `author = "Alice; touch PWNED #"` | `subprocess.run(f"git log --author={author}", shell=True)` | **File created** — command execution |
| `author = "--output=/path/PWNED"` | `subprocess.run(["git","log", author], shell=False)` | **File created** — git writes an attacker-chosen file |

The second is the point: **`shell=False` does not stop it.** No shell is involved; git itself parses
the value as an option. That is argument injection, and only validation stops it.

**Part B** fires the same payloads at the real tool. All nine are refused with `INVALID_INPUT`:

```
BLOCKED  shell injection (;)                  BLOCKED  path traversal (absolute)
BLOCKED  command substitution ($())           BLOCKED  path traversal (relative ..)
BLOCKED  pipe to shell (|)                    BLOCKED  NUL byte in path
BLOCKED  argument injection (--output=)       BLOCKED  oversized request (DoS)
BLOCKED  argument injection (--upload-pack=)
Files created by Part B: none
```

**Part C** runs a legitimate call, to show the tool still works.

Six layers do the work:

1. `shell=False` with an argv list — metacharacters are never interpreted
2. Allow-list validation — reject rather than escape; escaping is easy to get subtly wrong
3. `reject_option_like()` — no leading `-`; this is what stops argument injection
4. Flag and value as one token — `--author=<value>`, never `--author <value>`
5. Path confinement — resolve symlinks and `..` *first*, then check containment in `WORKSPACE_ROOT`
6. Bounds — `max_count` 1–200, title 256 chars, body 8000 chars, output 20k

### Best Practice #2 — Rate Limiting (`devworkflow/ratelimit.py`)

**Threat:** an MCP tool is called by a model *in a loop* — a retry loop, a prompt injection saying
"keep trying", or a plain agent bug. `create_github_issue` becomes an issue-spam bot and burns the
token's 5,000 req/hour GitHub quota.

**Token bucket.** The bucket holds `capacity` tokens and refills continuously at `capacity / window`
per second. Each call spends one; a call with no token is refused and told how long to wait. This
allows a short burst while bounding the long-run average, unlike a fixed-window counter which allows
`2N` calls across a boundary. `time.monotonic()` means a clock change cannot hand out free tokens,
and a `threading.Lock` makes check-then-act atomic.

```
PART A  without rate limiting : 8/8 writes would reach GitHub
PART B  with rate limiting    : 3 ALLOWED, 5 REFUSED
        call 4: REFUSED  code=RATE_LIMITED retry_after=10.0s
PART C  after waiting 10.4s   : ALLOWED again — a legitimate user is not locked out
PART D  git_read 10/10, github_write 0.04/3, github_read 20/20
```

Part D is why buckets are per tool: a write burst does not starve local reads. `dry_run=True`
throughout, so no real issues are created — the limiter runs *before* that flag is read.

**Reporting a refusal.** Neither guard rail may leak while saying no. Every refusal returns through
one envelope in `errors.py`: expected errors name the rule that was broken, unexpected ones return
only a random `error_id` with the traceback sent to stderr, and `Settings.redact()` scrubs the token
from any string on its way out. A GitHub `404` never reveals whether a private repo exists.

---

## 6. Project Structure

```
mcp-devworkflow/
├── server.py                    MCP entry point: 3 tools, 1 resource, 2 prompts
├── devworkflow/
│   ├── config.py                pathlib + dotenv_values; secrets never enter os.environ
│   ├── security.py              BEST PRACTICE 1 — validation & sanitization
│   ├── ratelimit.py             BEST PRACTICE 2 — token bucket
│   ├── errors.py                error taxonomy + safe error envelope (stderr logging)
│   ├── git_ops.py               subprocess wrapper: shell=False, timeout, minimal env
│   └── github_api.py            authenticated GitHub REST client (httpx)
├── exploits/
│   ├── exploit_injection.py     attacks vulnerable code, then the hardened tool
│   └── exploit_rate_limit.py    runaway-agent burst against the write tool
├── tests/
│   ├── test_security_and_ratelimit.py   14 unit tests
│   └── smoke_test_stdio.py      end-to-end MCP client over stdio
├── claude_desktop_config.json   MCP configuration (venv python)
├── claude_desktop_config.uv.json  MCP configuration (uv)
├── .env.example                 template — copy to .env, never commit .env
├── requirements.txt
└── README.md
```

`server.py` holds only MCP wiring, so tool definitions stay readable. Each guard rail is its own
module, so "where is input validated?" has one answer. The vulnerable code used to prove the
exploits lives in `exploits/` and is never imported by the server.

---

## 7. Code Citations

Every borrowed idea is cited here and again as a comment at the point of use.

| Where | What was used | Source |
|---|---|---|
| `server.py` | FastMCP skeleton: `FastMCP(...)`, `@mcp.tool()` / `@mcp.resource()` / `@mcp.prompt()` decorators, `mcp.run(transport="stdio")` under `__main__` | [modelcontextprotocol/python-sdk — Quickstart](https://github.com/modelcontextprotocol/python-sdk#quickstart) |
| `tests/smoke_test_stdio.py` | `stdio_client` + `ClientSession` boilerplate | [modelcontextprotocol/python-sdk — Writing MCP Clients](https://github.com/modelcontextprotocol/python-sdk#writing-mcp-clients) |
| `security.resolve_repo_path` | Resolve-then-check containment pattern, re-implemented in Python with `pathlib` (original is TypeScript) | [modelcontextprotocol/servers — filesystem server](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem) |
| `ratelimit.TokenBucket` | Token-bucket algorithm (continuous refill formulation), implemented from the algorithm description | [Token bucket — Wikipedia](https://en.wikipedia.org/wiki/Token_bucket) |
| `github_api.py` | Request headers (`Accept: application/vnd.github+json`, `X-GitHub-Api-Version`) and the `POST /repos/{owner}/{repo}/issues` contract | [GitHub REST API — Create an issue](https://docs.github.com/en/rest/issues/issues#create-an-issue) |
| `.env` handling | `dotenv_values()` rather than `load_dotenv()`, to keep secrets out of `os.environ` | [python-dotenv docs](https://saurabh-kumar.com/python-dotenv/) |

---

## 8. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Server not listed in Claude Desktop | Paths in `claude_desktop_config.json` must be absolute; quit Claude fully (Cmd+Q) and reopen |
| "Server disconnected" | Run `python tests/smoke_test_stdio.py` to see the real error. Logs: `~/Library/Logs/Claude/mcp-server-devworkflow.log` |
| `ModuleNotFoundError: mcp` | The config points at the system Python. Use `.venv/bin/python` |
| Every tool fails with a JSON parse error | Something wrote to **stdout**. stdout is the JSON-RPC channel — use the stderr logger in `errors.py`, never `print()` |
| `AUTH_ERROR` from GitHub | Token missing, expired, or lacking *Issues: Read and write*. Check with `server_diagnostics` |
| `repo_path is outside this server's workspace` | Working as designed — move the repo under `WORKSPACE_ROOT` or change it in `.env` |
| `RATE_LIMITED` during a real demo | Also working as designed. Wait the `retry_after_seconds`, or raise `WRITE_RATE_CAPACITY` in `.env` |

---
