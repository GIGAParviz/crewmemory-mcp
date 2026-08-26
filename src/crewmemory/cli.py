from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crewmemory",
        description="Shared crew memory MCP server for AI coding agents.",
    )
    sub = p.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="run the MCP server on stdio (default)")
    run.add_argument("--check", action="store_true", help="verify config + connectivity and exit")

    inst = sub.add_parser("install", help="one-command install into an AI coding agent")
    inst.add_argument("client", choices=("claude-code", "claude-desktop", "codex", "cursor", "gemini", "opencode", "windsurf"))
    inst.add_argument("--repo", default="", help="memory repo URL, e.g. https://github.com/org/crewmemory.git")
    inst.add_argument("--user", default="", help="your name (identity in crew memory)")
    inst.add_argument("--token", default="", help="GitHub token for private repos")
    inst.add_argument("--email", default="", help="commit email")
    inst.add_argument("--branch", default="", help="pin a branch")
    inst.add_argument("--project-path", default="", help="path to your code repo for project-aware features")
    inst.add_argument("--from-git-config", action="store_true", help="derive user/email/project from current git repo")
    inst.add_argument(
        "--launcher",
        choices=("current", "uvx"),
        default="current",
        help="server launcher saved in client config (uvx is portable after package publication)",
    )
    inst.add_argument(
        "--package",
        default="crewmemory-mcp",
        help="package or URL passed to uvx when --launcher=uvx",
    )

    init = sub.add_parser("init", help="register the current project for project-aware memory")
    init.add_argument("path", nargs="?", default="", help="project path (default: cwd)")
    init.add_argument("--force", action="store_true", help="allow non-git directories")

    sub.add_parser("doctor", help="diagnose configuration, connectivity and project detection")

    ui = sub.add_parser("ui", help="open the human dashboard: who did what, who's doing what, all features")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("--no-browser", action="store_true")
    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    cmd = args.cmd or "run"

    if cmd == "install":
        sys.exit(__import__("crewmemory.installer", fromlist=["cmd_install"]).cmd_install(args))
    if cmd == "init":
        sys.exit(__import__("crewmemory.installer", fromlist=["cmd_init"]).cmd_init(args))
    if cmd == "doctor":
        sys.exit(__import__("crewmemory.installer", fromlist=["cmd_doctor"]).cmd_doctor(args))
    if cmd == "ui":
        from .ui import cmd_ui

        sys.exit(cmd_ui(args))

    from .server import run_check

    if getattr(args, "check", False):
        sys.exit(run_check())
    from . import server

    server.main_run()


if __name__ == "__main__":
    main()
