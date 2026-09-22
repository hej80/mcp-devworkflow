# devworkflow — an MCP server for Development Workflow

**Author: Hamdan Emad Jabr Jaarour** · GitHub: hej80

A local [Model Context Protocol](https://modelcontextprotocol.io/) server that lets Claude Desktop read a local git repository and file GitHub issues on your behalf, with **input sanitization** and **rate limiting** enforced on every call.

| | |
|---|---|
| **Domain** | Development Workflow (git history + GitHub issue management) |
| **Exposes** | 3 tools, 1 resource, 2 prompts |
| **Authentication** | GitHub fine-grained Personal Access Token, loaded from `.env` |
| **Best practice #1** | Input sanitization (command injection, argument injection, path traversal) |
| **Best practice #2** | Rate limiting (token bucket, per tool) |
| **Bonus practice** | Error disclosure control (safe messages + operator-only `error_id`) |
| **Transport** | stdio (JSON-RPC), the transport Claude Desktop uses |

---

## 1. What the server exposes

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

Read-only GitHub repository metadata rendered as Markdown — description, default branch, visibility, language, open issue count, stars, last update. Example: `repo://modelcontextprotocol/python-sdk/summary`.

### Prompts

| Prompt | Purpose |
|---|---|
| `weekly_standup(repo_path, author, days)` | Turns commit history into a Shipped / In progress / Risks / Next step stand-up note |
| `triage_bug_report(raw_report, repo)` | Turns a messy bug report into a structured issue, previews it with `dry_run=true`, and requires user confirmation before filing |

---

## 2. How a request flows

Every tool call passes through the same four stages, in this order:

1. **Rate limit** (`ratelimit.py`) — token bucket, one budget per tool
2. **Validate** (`security.py`) — allow-list charsets, no leading `-`, path confined to `WORKSPACE_ROOT`
3. **Execute** — `git_ops.py` (subprocess, `shell=False`) or `github_api.py` (httpx + Bearer token)
4. **Reply** (`errors.py`) — uniform `{ok, error:{code, message}}` envelope, no tracebacks or secrets

**The order is deliberate.** The budget is spent *before* validation, so a runaway agent loop cannot
use the tool as an unlimited validation oracle; validation runs before any subprocess or HTTP call,
so no attacker-influenced value reaches `git` or `httpx`.

## 3. Setup

### Prerequisites

- Python 3.10 or newer (`python3 --version`)
- `git` on your PATH
- [Claude Desktop](https://claude.ai/download)
- [uv](https://docs.astral.sh/uv/) — install with `curl -LsSf https://astral.sh/uv/install.sh | sh`

### 3.1 Clone and create the virtual environment

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

In VS Code or Cursor: **Cmd/Ctrl+Shift+P → Python: Select Interpreter → `./.venv/bin/python`**, so the integrated terminal activates it automatically.

### 3.2 Create a GitHub token

1. Go to **[github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)** (fine-grained token).
2. **Repository access** → *Only select repositories* → pick the repo you want to file issues in.
3. **Permissions** → *Repository permissions* → **Issues: Read and write**. (Metadata: Read-only is added for you.)
4. Set a short expiry (30 days is plenty for this project) and click **Generate token**.
5. Copy the token — GitHub shows it exactly once.

Fine-grained over classic on purpose: a classic token with `repo` scope can read and write **every** repository you own; this one can only touch the repo you picked, and only its issues.

### 3.3 Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

```ini
GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxx
WORKSPACE_ROOT=/Users/hamdanjaarour/mcp-workspace
ALLOWED_REPOS=hej80/mcp-test
```

Then create the workspace and put a repo in it:

```bash
mkdir -p ~/mcp-workspace
cd ~/mcp-workspace && git clone https://github.com/hej80/mcp-test.git
```

`WORKSPACE_ROOT` is a sandbox: the git tool refuses any path that resolves outside it. `.env` is in `.gitignore` and must never be committed.

### 3.4 Verify before wiring up Claude Desktop

Run everything at once:

```bash
bash verify.sh
```

Or individually:

```bash
python tests/test_security_and_ratelimit.py   # 14 unit tests
python tests/smoke_test_stdio.py              # starts the server, speaks MCP to it
```

`smoke_test_stdio.py` launches `server.py` as a subprocess and talks JSON-RPC over stdio exactly as Claude Desktop does. If it prints **PASS**, Claude Desktop will be able to start the server.

---

## 4. Connect to Claude Desktop

Open the config file:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

Claude Desktop menu: **Settings → Developer → Edit Config** opens it directly.

Paste the contents of [`claude_desktop_config.json`](claude_desktop_config.json) from this repo, replacing both paths with your own **absolute** paths:

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

- **Paths must be absolute.** Claude Desktop does not run your shell, so `python`, `~` and relative paths do not resolve.
- **Use the venv's interpreter** (`.venv/bin/python`), not the system one, or the `mcp` package will not be found.
- **No secrets in this file.** The token stays in `.env`. `claude_desktop_config.json` is world-readable plaintext, and committing it with an `env` block containing a token is the standard way these tokens leak.

Then **quit Claude Desktop completely** (Cmd+Q — closing the window is not enough) and reopen it. The tools appear under the **search-and-tools icon** in the message box; the prompts appear under the **+** button as `weekly_standup` and `triage_bug_report`.

Ask Claude: *"Run server_diagnostics and tell me if my GitHub token works."*

---

## 5. Demonstrating the two best practices

```bash
python exploits/exploit_injection.py    # ~2 seconds
python exploits/exploit_rate_limit.py   # ~11 seconds
```

### Best practice #1 — Input sanitization (`devworkflow/security.py`)

Tool arguments are written by an LLM whose context may contain attacker-controlled text (a README, an issue body, a pasted log), so they are untrusted input exactly like an HTTP form field.

`exploit_injection.py` does two things in order.

**Part A proves the attacks are real** against deliberately vulnerable code that exists only inside the exploit file:

| Attack | Vulnerable implementation | Result |
|---|---|---|
| `author = "Alice; touch PWNED #"` | `subprocess.run(f"git log --author={author}", shell=True)` | **File created** — arbitrary command execution |
| `author = "--output=/path/PWNED"` | `subprocess.run(["git","log", author], shell=False)` | **File created** — `git log --output=` writes an attacker-chosen file |

The second one is the interesting case: **`shell=False` does not stop it.** No shell is involved at all; git itself parses the value as an option. This is argument injection, and only validation stops it.

**Part B fires the same payloads at the real tool.** All nine are refused with `INVALID_INPUT` and no file is created:

```
BLOCKED  shell injection (;)                  BLOCKED  path traversal (absolute)
BLOCKED  command substitution ($())           BLOCKED  path traversal (relative ..)
BLOCKED  pipe to shell (|)                    BLOCKED  NUL byte in path
BLOCKED  argument injection (--output=)       BLOCKED  oversized request (DoS)
BLOCKED  argument injection (--upload-pack=)
Files created by Part B: none
```

Part C then runs a legitimate call to show the tool still works.

The defences, in layers:

1. **`shell=False` with an argv list** (`git_ops.run_git`) — metacharacters are never interpreted.
2. **Allow-list validation** (`security.validate_author`, `REPO_SLUG_RE`) — reject rather than escape, because escaping is easy to get subtly wrong.
3. **`reject_option_like()`** — a value may not start with `-`, which is what stops argument injection.
4. **Flag and value as one token** (`--author=<value>`, never `--author <value>`).
5. **Path confinement** (`resolve_repo_path`) — resolve symlinks and `..` *first*, then check containment in `WORKSPACE_ROOT`.
6. **Bounds** — `max_count` 1–200, title 256 chars, body 8000 chars, output truncated at 20k.

### Best practice #2 — Rate limiting (`devworkflow/ratelimit.py`)

**Threat:** an MCP tool is called by a model *in a loop*. A retry loop, a prompt injection that says "keep trying", or a plain agent bug turns `create_github_issue` into an issue-spam bot and burns the token's 5,000 req/hour GitHub quota.

**Algorithm: token bucket.** The bucket holds `capacity` tokens and refills continuously at `capacity / window` tokens per second. Each call spends one; a call with no token is refused and told exactly how long to wait. A bucket permits a short burst (good UX) while bounding the long-run average — unlike a fixed-window counter, which allows `2N` calls across a window boundary. `time.monotonic()` is used so an NTP or DST clock change cannot hand out free tokens, and a `threading.Lock` makes check-then-act atomic.

`exploit_rate_limit.py` simulates the runaway loop with 8 calls in a row:

```
PART A  without rate limiting : 8/8 writes would reach GitHub
PART B  with rate limiting    : 3 ALLOWED, 5 REFUSED
        call 4: REFUSED  code=RATE_LIMITED retry_after=10.0s
PART C  after waiting 10.4s   : ALLOWED again — a legitimate user is not locked out
PART D  git_read 10/10, github_write 0.04/3, github_read 20/20
```

Part D is the point of per-tool buckets: a write burst does not starve local reads. `dry_run=True` is used throughout so the demo never creates real issues — the limiter runs *before* that flag is read, so the budget behaves exactly as it would for a real write.

### Bonus — Error disclosure (`devworkflow/errors.py`)

Expected errors return a message describing the *rule* that was broken. Unexpected exceptions return only a random `error_id`; the traceback goes to the stderr log, which only the operator can read. A GitHub `404` never says whether a private repo exists, and `Settings.redact()` scrubs the token from any string on its way out.

---

## 6. Project structure

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

**Why this layout.** `server.py` holds only MCP wiring, so the tool definitions stay readable. Each guard rail is its own module, so "where is input validated?" has exactly one answer. The vulnerable code used to prove the exploits lives in `exploits/` and is never imported by the server.

---

## 7. Code citations

Every borrowed idea is cited here and again as a comment at the point of use.

| Where | What was used | Source |
|---|---|---|
| `server.py` | FastMCP skeleton: `FastMCP(...)`, `@mcp.tool()` / `@mcp.resource()` / `@mcp.prompt()` decorators, `mcp.run(transport="stdio")` under `__main__` | [modelcontextprotocol/python-sdk — Quickstart](https://github.com/modelcontextprotocol/python-sdk#quickstart) |
| `tests/smoke_test_stdio.py` | `stdio_client` + `ClientSession` boilerplate | [modelcontextprotocol/python-sdk — Writing MCP Clients](https://github.com/modelcontextprotocol/python-sdk#writing-mcp-clients) |
| `security.resolve_repo_path` | Resolve-then-check containment pattern, re-implemented in Python with `pathlib` (original is TypeScript) | [modelcontextprotocol/servers — filesystem server](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem) |
| `ratelimit.TokenBucket` | Token-bucket algorithm (continuous refill formulation), implemented from the algorithm description | [Token bucket — Wikipedia](https://en.wikipedia.org/wiki/Token_bucket) |
| `github_api.py` | Request headers (`Accept: application/vnd.github+json`, `X-GitHub-Api-Version`) and the `POST /repos/{owner}/{repo}/issues` contract | [GitHub REST API — Create an issue](https://docs.github.com/en/rest/issues/issues#create-an-issue) |
| `.env` handling | `dotenv_values()` rather than `load_dotenv()`, to keep secrets out of `os.environ` | [python-dotenv docs](https://saurabh-kumar.com/python-dotenv/) |

An AI assistant (Claude) was used while writing this project, as the assignment requires. Every line was reviewed and is explainable; the architecture decisions (rate-limit-before-validate ordering, `dotenv_values` over `load_dotenv`, per-tool buckets, argument-injection guard) are documented in the code comments and in the reflection.

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

## 9. Known limitations

| Limitation | Why | How I would fix it |
|---|---|---|
| **Rate limits reset on restart** | The buckets live in process memory, and two clients each running their own copy each get a full budget — so the real limit is N times the configured one | Store the counters in Redis or SQLite so every process shares one budget |
| **No per-caller identity** | stdio MCP has no notion of a user, so the budget is global to the server | An HTTP transport with OAuth, keying a bucket per authenticated caller |
| **Issue creation is not idempotent** | If GitHub succeeds but the response is lost to a timeout, a retry files a duplicate | An idempotency key hashing repo + title + body, with a short-lived cache of recent issues |
| **ASCII-only validation** | The allow-list rejects legitimate names containing accents or non-Latin scripts | Unicode NFKC normalization, then reject by character *category* rather than by alphabet |
| **Sandbox enforced in Python, not the OS** | `resolve_repo_path()` is correct — it resolves symlinks before checking containment — but a bug in my own code would bypass it | Run the server in a container with only the workspace mounted, so the kernel enforces the boundary |

The last one is different in kind from the others. The first four are things I would build with
more time; the fifth is the difference between a control that is *correct* and one that is
*trustworthy*. Mine is correct, but it asks you to trust that my code has no bugs — a container
does not.
