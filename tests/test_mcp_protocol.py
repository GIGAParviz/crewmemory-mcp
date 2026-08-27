from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpProtocolTests(unittest.TestCase):
    def test_stdio_initialize_tools_and_git_backed_write(self) -> None:
        base = Path.cwd() / ".test-tmp"
        base.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base, ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            remote = root / "remote.git"
            project = root / "sample-project"
            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "init", "-b", "main", str(project)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(project), "config", "user.name", "Tester"], check=True)
            subprocess.run(["git", "-C", str(project), "config", "user.email", "tester@example.test"], check=True)
            (project / "README.md").write_text("sample\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(project), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-m", "init"], check=True, capture_output=True)

            env = {
                "CREWMEMORY_REPO_URL": remote.as_uri(),
                "CREWMEMORY_USER": "protocol-tester",
                "CREWMEMORY_LOCAL_PATH": str(root / "memory-local"),
                "CREWMEMORY_HOME": str(root / "home"),
            }

            async def exercise() -> None:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "crewmemory.cli", "run"],
                    env=env,
                    cwd=str(project),
                )
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        initialized = await session.initialize()
                        self.assertEqual(initialized.server_info.name, "crewmemory")
                        self.assertIn("self-contained", initialized.instructions or "")
                        self.assertIn("Evidence", initialized.instructions or "")
                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        self.assertIn("team_context", names)
                        self.assertIn("save_note", names)

                        context = await session.call_tool("team_context", {"project_path": str(project)})
                        context_text = context.content[0].text
                        self.assertIn("project: sample-project", context_text)
                        repeated_context = await session.call_tool("team_context", {})
                        self.assertIn("project: sample-project", repeated_context.content[0].text)
                        saved = await session.call_tool(
                            "save_note",
                            {"title": "Protocol smoke", "content": "written through MCP", "tags": ["test"]},
                        )
                        self.assertIn("Saved note", saved.content[0].text)
                        stats = await session.call_tool("memory_stats", {})
                        self.assertIn("note:1", stats.content[0].text)

            asyncio.run(exercise())
            tree = subprocess.run(
                ["git", "--git-dir", str(remote), "ls-tree", "-r", "--name-only", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("notes/", tree)


if __name__ == "__main__":
    unittest.main()
