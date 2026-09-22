"""
End-to-end smoke test: launches server.py as a subprocess and talks to it over
stdio with the real MCP client, exactly as Claude Desktop does.

Run:  python tests/smoke_test_stdio.py

This is the check to run before a demo. If it prints PASS, Claude Desktop will
be able to start the server too.

The client-session boilerplate follows the MCP Python SDK README's client
example: https://github.com/modelcontextprotocol/python-sdk#writing-mcp-clients
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client             # noqa: E402

GREEN, RED, BOLD, RESET = "\033[92m", "\033[91m", "\033[1m", "\033[0m"


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_ROOT / "server.py")],
        cwd=str(PROJECT_ROOT),
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            print(f"{BOLD}Connected to:{RESET} {info.serverInfo.name} "
                  f"v{info.serverInfo.version}")

            tools = await session.list_tools()
            print(f"\n{BOLD}Tools ({len(tools.tools)}){RESET}")
            for tool in tools.tools:
                first_line = (tool.description or "").strip().splitlines()[0]
                print(f"  - {tool.name}: {first_line}")

            templates = await session.list_resource_templates()
            print(f"\n{BOLD}Resource templates ({len(templates.resourceTemplates)}){RESET}")
            for template in templates.resourceTemplates:
                print(f"  - {template.uriTemplate}")

            prompts = await session.list_prompts()
            print(f"\n{BOLD}Prompts ({len(prompts.prompts)}){RESET}")
            for prompt in prompts.prompts:
                args = ", ".join(a.name for a in (prompt.arguments or []))
                print(f"  - {prompt.name}({args})")

            print(f"\n{BOLD}Calling server_diagnostics (offline){RESET}")
            result = await session.call_tool("server_diagnostics",
                                             {"check_github": False})
            print("  " + result.content[0].text.replace("\n", "\n  ")[:600])

            print(f"\n{BOLD}Calling summarize_recent_commits with a malicious "
                  f"author (should be refused){RESET}")
            result = await session.call_tool(
                "summarize_recent_commits",
                {"repo_path": "/etc", "author": "x; rm -rf ~"},
            )
            text = result.content[0].text
            refused = "INVALID_INPUT" in text
            print(f"  {GREEN + 'refused as expected' + RESET if refused else RED + 'NOT refused' + RESET}")
            print("  " + text.replace("\n", "\n  ")[:300])

            print(f"\n{BOLD}Rendering the weekly_standup prompt{RESET}")
            rendered = await session.get_prompt(
                "weekly_standup", {"repo_path": "~/mcp-workspace/demo-repo"}
            )
            print("  " + rendered.messages[0].content.text[:200].replace("\n", "\n  ") + " ...")

            if refused:
                print(f"\n{GREEN}{BOLD}PASS: server started, advertised its "
                      f"capabilities, answered calls and enforced validation.{RESET}")
                return 0
            print(f"\n{RED}{BOLD}FAIL{RESET}")
            return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
