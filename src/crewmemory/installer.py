from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from .config import ConfigError, load_config, save_connection

CLIENTS = ("claude-code", "claude-desktop", "codex", "cursor", "gemini", "opencode", "windsurf")

SERVER_KEY = "crewmemory"


def exe_path() -> str:
    arg0 = Path(sys.argv[0]).resolve()
    if arg0.name in ("crewmemory", "crewmemory.exe") and arg0.exists():
        return str(arg0)
    found = shutil.which("crewmemory")
    return found or str(arg0)


def launch_spec(launcher: str = "current", package: str = "crewmemory-mcp") -> tuple[str, list[str]]:
    if launcher == "uvx":
        return shutil.which("uvx") or "uvx", ["--from", package, "crewmemory"]
    return exe_path(), []


def build_env(repo: str, user: str, token: str, email: str, branch: str, project_path: str) -> dict:
    env = {"CREWMEMORY_REPO_URL": repo, "CREWMEMORY_USER": user}
    if token:
        env["CREWMEMORY_TOKEN"] = token
    if email:
        env["CREWMEMORY_EMAIL"] = email
    if branch:
        env["CREWMEMORY_BRANCH"] = branch
    if project_path:
        env["CREWMEMORY_PROJECT_PATH"] = project_path
    return env


def _home_file(rel: str) -> Path:
    p = Path.home() / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def claude_desktop_path() -> Path:
    if sys.platform == "darwin":
        rel = "Library/Application Support/Claude/claude_desktop_config.json"
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming"))
        rel = str(Path(appdata) / "Claude" / "claude_desktop_config.json")
        p = Path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    else:
        rel = ".config/Claude/claude_desktop_config.json"
    return _home_file(rel)


def _merge_json(path: Path, mutator) -> bool:
    existed = path.exists()
    data = {}
    if existed:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup)
        except json.JSONDecodeError:
            print(f"  ! {path} is not valid JSON; writing fresh file (old kept as .invalid)")
            shutil.copy2(path, path.with_suffix(path.suffix + ".invalid"))
            data = {}
            existed = False
    before = json.dumps(data, sort_keys=True)
    mutator(data)
    after = json.dumps(data, sort_keys=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return not existed or before != after


def _std_mcp_entry(env: dict, command: str, args: list[str]) -> dict:
    entry = {"command": command, "env": env}
    if args:
        entry["args"] = args
    return entry


def install_json_client(
    name: str,
    rel_or_fn,
    env: dict,
    wrapper=None,
    launch: tuple[str, list[str]] | None = None,
) -> Path:
    path = rel_or_fn() if callable(rel_or_fn) else _home_file(rel_or_fn)
    command, args = launch or launch_spec()

    def mutate(data):
        if name == "opencode":
            # OpenCode v2 keeps local servers under mcp.servers, rather than
            # directly under mcp.
            section = data.setdefault("mcp", {}).setdefault("servers", {})
            entry = {"type": "local", "command": [command, *args], "environment": env}
        else:
            section = data.setdefault("mcpServers", {})
            entry = _std_mcp_entry(env, command, args) if wrapper is None else wrapper(env)
        section[SERVER_KEY] = entry

    changed = _merge_json(path, mutate)
    print(f"[{'updated' if changed else 'unchanged'}] {path}")
    return path


def install_codex(env: dict, launch: tuple[str, list[str]] | None = None) -> Path:
    path = _home_file(".codex/config.toml")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    command, args = launch or launch_spec()
    header = f"[mcp_servers.{SERVER_KEY}]"
    lines_out = []
    if header in text:
        keep = True
        for line in text.splitlines():
            if line.strip() == header:
                keep = False
                continue
            if not keep and line.strip().startswith("["):
                keep = True
            if keep:
                lines_out.append(line)
        text = "\n".join(lines_out).rstrip() + "\n\n"
    if text and not text.endswith("\n"):
        text += "\n"
    text += header + "\n"
    # JSON string escaping is compatible with TOML basic strings, including
    # Windows paths and values containing quotes or backslashes.
    text += f"command = {json.dumps(command, ensure_ascii=True)}\n"
    if args:
        text += f"args = {json.dumps(args, ensure_ascii=True)}\n"
    text += "env = { " + ", ".join(
        f"{json.dumps(k, ensure_ascii=True)} = {json.dumps(v, ensure_ascii=True)}"
        for k, v in env.items()
    ) + " }\n"
    path.write_text(text, encoding="utf-8")
    print(f"[written] {path}")
    return path


RESTART_HINTS = {
    "claude-code": "Restart Claude Code (or run /mcp to verify it loads).",
    "claude-desktop": "Quit & reopen the Claude desktop app.",
    "codex": "Restart codex.",
    "cursor": "Reload Cursor window (Cmd/Ctrl+Shift+P → Reload Window).",
    "gemini": "Restart the gemini CLI.",
    "opencode": "Restart opencode.",
    "windsurf": "Reload Windsurf window.",
}


def cmd_install(args) -> int:
    client = args.client
    if client not in CLIENTS:
        print(f"Unknown client '{client}'. Choose one of: {', '.join(CLIENTS)}")
        return 1

    repo = args.repo or os.environ.get("CREWMEMORY_REPO_URL", "").strip()
    user = args.user or os.environ.get("CREWMEMORY_USER", "").strip()
    token = args.token or os.environ.get("CREWMEMORY_TOKEN", "").strip()
    email = args.email
    branch = args.branch or os.environ.get("CREWMEMORY_BRANCH", "").strip()
    project_path = args.project_path

    if args.from_git_config or (not user):
        from .project import discover

        proj = discover(Path(project_path) if project_path else None)
        if proj and proj.available:
            user = user or (proj.user_name.replace(" ", "-").lower() if proj.user_name else "")
            email = email or proj.user_email
            if not project_path and proj.available:
                project_path = str(proj.path)
            print(f"Detected git identity: {proj.user_name} <{proj.user_email}> in {proj.path}")

    missing = [n for n, v in (("repo", repo), ("user", user)) if not v]
    if missing:
        print(
            f"Missing required value(s): {', '.join(missing)}.\n"
            "Pass them explicitly, e.g.:\n"
            f"  crewmemory install {client} --repo https://github.com/org/crewmemory.git --user alice [--token gh_pat_xxx]\n"
            "or run inside your code repo with a configured git identity and --from-git-config."
        )
        return 1

    env = build_env(repo, user, token or "", email or "", branch or "", project_path or "")
    launch = launch_spec(args.launcher, args.package)
    saved = save_connection(env)

    if client == "codex":
        install_codex(env, launch)
    elif client == "claude-desktop":
        install_json_client(client, claude_desktop_path, env, launch=launch)
    else:
        relmap = {
            "claude-code": ".claude.json",
            "cursor": ".cursor/mcp.json",
            "windsurf": ".codeium/windsurf/mcp_config.json",
            "gemini": ".gemini/settings.json",
            "opencode": ".config/opencode/opencode.json",
        }
        install_json_client(client, relmap[client], env, launch=launch)

    print(f"\ncrewmemory installed for {client} as '{SERVER_KEY}'.")
    print(f"Connection profile saved at {saved}.")
    print(RESTART_HINTS[client])
    print("Then just tell your agent things like 'remember that...' or ask 'what is the team working on?'.")
    return 0


def cmd_init(args) -> int:
    from .models import utc_now_iso
    from .project import discover

    root = Path(args.path).resolve() if args.path else Path.cwd()
    proj = discover(root)
    if not proj or not proj.available:
        print(
            f"'{root}' is not inside a git repository. Project memory works best in a git repo "
            "(branch-aware entries, blame, PR review). Run this inside your project, or use "
            "--force to skip."
        )
        if not args.force:
            return 1

    slug = proj.slug if proj and proj.available else root.name
    home = Path(os.environ.get("CREWMEMORY_HOME", "").strip() or Path.home() / ".crewmemory")
    home.mkdir(parents=True, exist_ok=True)
    reg = home / "projects.json"
    data = {}
    if reg.exists():
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data[slug] = {
        "path": str(root),
        "registered_at": utc_now_iso(),
        "git_user": proj.user_name if proj else "",
        "git_email": proj.user_email if proj else "",
    }
    reg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    profile_note = ""
    if proj and proj.available and (proj.user_name or proj.user_email):
        profile_note = f"\nGit identity found: {proj.user_name} <{proj.user_email}> — a member profile can be created by the agent via set_my_profile."

    print(
        f"Project initialized: {slug}\n"
        f"  path:     {root}\n"
        f"  branch:   {proj.branch if proj and proj.available else '-'}\n"
        f"  registry: {reg}{profile_note}\n\n"
        "Next steps:\n"
        f"  1) crewmemory install claude-code --repo <memory-repo-url> --user <name> --project-path \"{root}\"\n"
        "  2) restart your agent and say: 'load team context'"
    )
    return 0


def cmd_doctor(_args) -> int:
    ok = True
    print("crewmemory doctor")
    print("-" * 50)
    try:
        cfg = load_config()
        print(f"config:   OK (user={cfg.user}, repo={cfg.repo_url})")
    except ConfigError as exc:
        print(f"config:   MISSING -> {exc}")
        print("          fix with: crewmemory install <client> ...")
        return 1

    from .store import Store

    try:
        store = Store(cfg)
        c = store.counts()
        total = sum(c.values())
        print(f"team repo: OK at {store.path} (branch {store.branch}, {total} entries)")
    except Exception as exc:
        print(f"team repo: FAIL -> {exc}")
        ok = False

    try:
        pstore = Store.personal(cfg)
        print(f"personal:  OK at {pstore.path}")
    except Exception as exc:
        print(f"personal:  FAIL -> {exc}")

    from .project import discover

    proj = discover(cfg.project_path)
    if proj and proj.available:
        print(f"project:   detected '{proj.slug}' (branch {proj.branch}) at {proj.path}")
    else:
        print("project:   no code repository detected (code-aware features disabled)")

    if ok:
        print("\nAll good. Restart your agent if you just changed config.")
        return 0
    print("\nSome checks failed — see above.")
    return 1
