from __future__ import annotations

import argparse
import functools
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .config import ConfigError, load_config
from .intelligence import (
    find_duplicates as _find_duplicates_impl,
    pr_memory_review as _pr_review_impl,
    recall_pack as _recall_pack_impl,
    why_code as _why_code_impl,
)
from .models import KINDS, format_brief
from .project import ProjectRepo, discover as discover_project
from .store import Store, StoreError


DETAILED_MEMORY_STANDARD = (
    "Every TEAM entry must be self-contained and useful to a teammate who has not seen "
    "this chat. Prefer 150-500 words when there is substantive material. Include only "
    "known facts, in this order when applicable: Context (goal and scope); What changed "
    "or was learned; Evidence (tests, commands, or observed results); Why and trade-offs; "
    "Files or interfaces affected; Risks or limits; and Next steps. Do not pad routine "
    "status updates and never invent facts. Link exact files, commits, errors, and "
    "verification results when available."
)

mcp = MCPServer(
    name="crewmemory",
    instructions=(
        "You have CREW MEMORY: a git-backed shared memory connecting every teammate's AI agent "
        "(Claude Code, Codex, Cursor, ...). It has two scopes: TEAM memory (shared via GitHub, "
        "auto-pushed on save) and PERSONAL memory (private to this developer's machine). At session "
        "start call team_context(project_path='<absolute current workspace root>') so memories are "
        "tagged with the correct project and branch. Never store secrets.\n"
        "\n"
        "AUTOMATIC BEHAVIOR — do this without being asked:\n"
        "1. At session start call team_context(project_path='<absolute current workspace root>') once. "
        "It shows who is doing what and the latest memories for the current project. Then announce "
        "your work with update_status(task).\n"
        "2. Before researching anything the team may already know (setup, bugs, conventions, why "
        "code exists), use recall(query) — it ranks memories by relevance and confidence within a "
        "context budget.\n"
        "3. When you learn something durable — a fix, a decision + why, a gotcha, a pattern — SAVE "
        "it immediately with log_solution / log_decision / save_gotcha / save_pattern / save_note. "
        "Link files=[...] when relevant; teammates' tools use those links for PR review and decay. "
        + DETAILED_MEMORY_STANDARD
        + "\n"
        "4. When you switch tasks, update update_status(task, progress_percent, blockers).\n"
        "5. At session end (user says done/bye or switches context) write save_handoff(summary, "
        "next_steps, blockers, open_questions) so the next session/developer continues smoothly.\n"
        "6. Never store secrets/passwords in memory. Personal/private facts go in personal scope "
        "(scope='personal').\n"
        "\n"
        "Memory entries carry author, timestamps, project, branch, linked files/commits, tags and "
        "a lifecycle status (unverified → verified → superseded/stale). Confidence decays with age "
        "and when linked code changes. Prefer verified+recent entries; flag stale ones you disprove."
    ),
)

_team_store: Store | None = None
_personal_store: Store | None = None
_project_path_hint: Path | None = None


def get_store() -> Store:
    global _team_store
    if _team_store is None:
        _team_store = Store(load_config())
    return _team_store


def get_personal() -> Store:
    global _personal_store
    if _personal_store is None:
        _personal_store = Store.personal(load_config(), _project_path_hint)
    return _personal_store


def select_project(project_path: str) -> ProjectRepo | None:
    global _personal_store, _project_path_hint
    cleaned = project_path.strip()
    if cleaned:
        _project_path_hint = Path(cleaned).expanduser()
        project = get_store().set_project(_project_path_hint)
        _personal_store = None
        return project
    if _project_path_hint is not None:
        return get_store().project_git.repo
    return get_store().set_project(None)


def pick_store(scope: str) -> Store:
    return get_personal() if scope == "personal" else get_store()


def guard(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ConfigError as exc:
            return f"CONFIG ERROR: {exc}"
        except StoreError as exc:
            return f"CREW MEMORY ERROR: {exc}"

    return wrapper


def _changed_cache():
    try:
        return get_store().project_git.changed_files_since("1970-01-01T00:00:00Z")
    except Exception:
        return set()


# ---------------- session & retrieval ----------------

@mcp.tool()
@guard
def team_context(project_path: str = "") -> str:
    """Session-start briefing for the current project. Pass the absolute workspace/repo root in project_path so project and branch attribution stay correct across projects. Call this FIRST at every session start."""
    project = select_project(project_path)
    prefix = ""
    if project_path.strip() and project is None:
        prefix = f"WARNING: '{project_path}' is not inside a git repository; project-aware features are disabled.\n\n"
    return prefix + get_store().team_context()


@mcp.tool()
@guard
def recall(query: str, scope: str = "all", budget_chars: int = 4000) -> str:
    """Smart memory retrieval: keyword search ranked by relevance x recency x confidence, packed into a context budget. Searches team (+personal by default, scope='team'|'personal'|'all'). Prefer this over raw search when answering questions."""
    if not query.strip():
        return "ERROR: empty query."
    try:
        st = get_store()
        branch = st.project_git.current_branch()

        cache: dict = {}

        def changed_for(meta):
            files = meta.get("files") or []
            if not files or not st.available_project():
                return set()
            key = meta.get("created", "")
            if key not in cache:
                cache[key] = st.project_git.changed_files_since(key)
            return cache[key]

    except Exception:
        branch, changed_for = "", (lambda meta: set())
    pools = []
    if scope in ("all", "team"):
        pools.append(get_store())
    if scope in ("all", "personal"):
        pools.append(get_personal())
    entries = []
    for pool in pools:
        for meta, body in pool.all_meta_bodies():
            if meta.get("status") == "superseded":
                continue
            entries.append((meta, body))
    text, used = _recall_pack_impl(
        entries, query, budget_chars=max(500, min(budget_chars, 20000)), branch=branch, changed_resolver=changed_for
    )
    if not text:
        return (
            f"No memories match '{query}'. If you then learn it elsewhere, save it so the team has it next time."
        )
    return text


@mcp.tool()
@guard
def search_memory(
    query: str,
    kind: str | None = None,
    tags: list[str] | None = None,
    author: str | None = None,
    file: str | None = None,
    project: str | None = None,
    limit: int = 10,
) -> str:
    """Raw faceted search across crew memory. Filters: kind (note|decision|solution|gotcha|pattern|handoff), tags, author, file (path substring), project. Empty query with filters lists matches. Use recall() for smart ranking instead."""
    return get_store().search(query or "", kind, tags or [], author, file, project or "__current__", limit)


@mcp.tool()
@guard
def list_recent(kind: str | None = None, limit: int = 15, project: str | None = None) -> str:
    """List newest memories (optionally filter kind or an exact project name; default all projects)."""
    return get_store().list_recent(kind, limit, project or "__all__")


@mcp.tool()
@guard
def memory_stats() -> str:
    """Automatic index of everything in memory: counts by type, author, lifecycle status, project and top tags."""
    return get_store().stats()


# ---------------- writing ----------------

@mcp.tool()
@guard
def save_note(title: str, content: str, tags: list[str] | None = None, files: list[str] | None = None, scope: str = "team") -> str:
    """Save durable, detailed knowledge to shared memory and push instantly.

    Make ``content`` self-contained: explain context, durable learning, evidence, rationale or
    trade-offs, affected files/interfaces, limitations, and next steps when applicable. ``files``
    links related repo paths; ``scope='personal'`` keeps it private. NEVER store secrets.
    """
    entry, warns = pick_store(scope).save_entry("note", title, content, tags or [], files=files)
    out = f"Saved note '{entry.title}' as {entry.id}"
    out += " (local only)" if scope == "personal" else " and pushed to GitHub"
    return out + "." + ("\n" + "\n".join(warns) if warns else "")


@mcp.tool()
@guard
def log_decision(title: str, context: str, decision: str, rationale: str, tags: list[str] | None = None, files: list[str] | None = None) -> str:
    """Record a detailed architecture or tooling decision.

    State the triggering context, chosen approach, viable alternatives or trade-offs, expected
    impact, affected files/interfaces, and how the decision was verified or should be revisited.
    Keep all claims grounded in known facts.
    """
    body = f"## Context\n{context}\n\n## Decision\n{decision}\n\n## Rationale\n{rationale}"
    entry, warns = get_store().save_entry("decision", title, body, tags or [], files=files)
    return f"Logged decision '{entry.title}' as {entry.id} and pushed." + ("\n" + "\n".join(warns) if warns else "")


@mcp.tool()
@guard
def log_solution(problem: str, solution: str, error_text: str = "", tags: list[str] | None = None, files: list[str] | None = None) -> str:
    """Record a reusable bug fix or troubleshooting solution.

    Include reproduction conditions and symptoms, root cause when known, exact fix, affected
    files/interfaces, validation performed, and remaining caveats or follow-up. Preserve useful
    raw error text, but do not guess a root cause.
    """
    title = problem.strip().splitlines()[0][:80]
    parts = [f"## Problem\n{problem}"]
    if error_text.strip():
        parts.append(f"## Error output\n```\n{error_text.strip()[:2000]}\n```")
    parts.append(f"## Solution\n{solution}")
    entry, warns = get_store().save_entry("solution", title, "\n\n".join(parts), tags or [], files=files)
    return f"Logged solution '{entry.title}' as {entry.id} and pushed." + ("\n" + "\n".join(warns) if warns else "")


@mcp.tool()
@guard
def save_gotcha(gotcha: str, details: str = "", tags: list[str] | None = None, files: list[str] | None = None) -> str:
    """Save a documented warning about a recurring pitfall.

    Explain its trigger, symptoms, why it happens when known, safe mitigation, and recovery
    verification. Keep the title short, but make ``details`` self-contained and useful.
    """
    entry, warns = get_store().save_entry("gotcha", gotcha, details or gotcha, tags or [], files=files)
    return f"Saved gotcha '{entry.title}' as {entry.id} and pushed." + ("\n" + "\n".join(warns) if warns else "")


@mcp.tool()
@guard
def save_pattern(pattern_name: str, description: str, example: str = "", tags: list[str] | None = None, files: list[str] | None = None) -> str:
    """Save a reusable project pattern or preferred implementation approach.

    Describe when to use it, how to apply it, why it is preferred, affected interfaces, a
    concrete example when available, and situations where it should not be used.
    """
    body = description + (f"\n\n## Example\n{example}" if example else "")
    entry, warns = get_store().save_entry("pattern", pattern_name, body, tags or [], files=files)
    return f"Saved pattern '{entry.title}' as {entry.id} and pushed." + ("\n" + "\n".join(warns) if warns else "")


@mcp.tool()
@guard
def save_handoff(summary: str, next_steps: str, blockers: list[str] | None = None, open_questions: list[str] | None = None) -> str:
    """Write a detailed session handoff for the next teammate or agent.

    Include goal, completed work, current state, exact files changed, tests/results, remaining
    risks, and precise next actions. Put blockers and unanswered questions in their dedicated
    fields; never imply work was verified when it was not.
    """
    parts = [f"## Summary\n{summary}", f"## Next steps\n{next_steps}"]
    if blockers:
        parts.append("## Blockers\n" + "\n".join(f"- {b}" for b in blockers))
    if open_questions:
        parts.append("## Open questions\n" + "\n".join(f"- {q}" for q in open_questions))
    entry, _ = get_store().save_entry("handoff", f"Handoff {summary.strip()[:50]}", "\n\n".join(parts), ["handoff"])
    return f"Handoff written as {entry.id} and pushed. Next session can start with latest_handoff."


@mcp.tool()
@guard
def latest_handoff(author: str | None = None) -> str:
    """Get the most recent handoff document (anyone's, or filter by author). Ideal first read when continuing someone's work."""
    st = get_store()
    items = []
    for meta, body in st.all_meta_bodies():
        if meta.get("type") != "handoff":
            continue
        if author and meta.get("author", "").lower() != author.lower():
            continue
        items.append((meta.get("created", ""), meta, body))
    if not items:
        return "No handoffs saved yet."
    items.sort(key=lambda x: x[0], reverse=True)
    _, meta, body = items[0]
    from .models import format_full

    return format_full(meta, body)


@mcp.tool()
@guard
def remember_commit_digest(base_ref: str, head_ref: str = "HEAD") -> str:
    """Get a formatted commit log + changed-files summary of base..head from YOUR CODE repo, ready to summarize into a note/decision with save_note/log_decision. The AI writes the actual summary."""
    st = get_store()
    if not st.available_project():
        return "No code repository detected (run inside your project or set CREWMEMORY_PROJECT_PATH)."
    digest = st.project_git.commit_log(base_ref, head_ref)
    if digest is None:
        return f"ERROR: could not compute log for {base_ref}..{head_ref}."
    return f"Commit digest {base_ref}..{head_ref}:\n\n{digest}\n\nSummarize the important parts and save with save_note/log_decision (link files where useful)."


# ---------------- lifecycle / management ----------------

@mcp.tool()
@guard
def get_memory(entry_id: str) -> str:
    """Full text of one memory by id (unique prefix ok) including confidence score and lifecycle status."""
    st = get_store()
    changed = set()
    try:
        found = st._find_entry_file(entry_id)
        if found and (found[1].get("files")) and st.available_project():
            changed = st.project_git.changed_files_since(found[1].get("created", ""))
    except Exception:
        pass
    return st.get_entry(entry_id, changed)


def _changed_cache():
    try:
        return get_store().project_git.changed_files_since("1970-01-01T00:00:00Z")
    except Exception:
        return set()


@mcp.tool()
@guard
def delete_memory(entry_id: str) -> str:
    """Delete your own memory entry. For other people's entries use mark_superseded instead."""
    return get_store().delete_entry(entry_id)


@mcp.tool()
@guard
def verify_memory(entry_id: str) -> str:
    """Mark a memory as verified (you checked it is still true). Verified memories rank higher and decay slower."""
    return get_store().set_lifecycle(entry_id, "verified")


@mcp.tool()
@guard
def mark_superseded(entry_id: str, replaced_by_id: str = "") -> str:
    """Mark an outdated memory as superseded (optionally pointing to the newer replacement id). It stops appearing in recall but stays in history."""
    return get_store().set_lifecycle(entry_id, "superseded", replaced_by_id)


@mcp.tool()
@guard
def flag_stale(entry_id: str) -> str:
    """Flag a memory as stale (probably wrong now, needs re-check). Stale entries are down-ranked heavily."""
    return get_store().set_lifecycle(entry_id, "stale")


@mcp.tool()
@guard
def find_duplicates() -> str:
    """Detect duplicate/overlapping memories (similar titles or same linked files) so they can be consolidated."""
    return _find_duplicates_impl(get_store().all_meta_bodies())


@mcp.tool()
@guard
def entry_history(entry_id: str) -> str:
    """Provenance: every git commit that touched this memory — who changed it, when, and why (commit messages)."""
    return get_store().entry_history(entry_id)


@mcp.tool()
@guard
def memory_at(ref: str, kind: str | None = None, limit: int = 25) -> str:
    """Time travel: read crew memory AS OF a specific commit SHA, tag or branch of the memory repo. Great for 'what did we know back then'."""
    return get_store().memory_at(ref, kind, limit)


@mcp.tool()
@guard
def sync_memory(direction: str = "both") -> str:
    """Manual sync with GitHub: direction='pull'|'push'|'both'. Useful after working offline (writes made offline are queued locally and pushed here)."""
    return get_store().sync_memory(direction)


# ---------------- presence / team ----------------

@mcp.tool()
@guard
def update_status(task: str, progress: int | None = None, blockers: list[str] | None = None) -> str:
    """Announce what you're working on for the whole team: task description, optional progress percent, optional blockers list. Call at session start and whenever the task changes. Teammates see it instantly."""
    return get_store().update_status(task, progress, blockers or [])


@mcp.tool()
@guard
def get_team_status() -> str:
    """See everyone's current task, progress %, blockers and staleness. Read-only."""
    return get_store().get_team_status()


@mcp.tool()
@guard
def recent_activity(limit: int = 30, author: str | None = None) -> str:
    """Timeline of crew memory activity: statuses, saves, lifecycle changes — newest first, optionally per author."""
    return get_store().recent_activity(limit, author)


@mcp.tool()
@guard
def set_my_profile(role: str = "", timezone: str = "", about: str = "") -> str:
    """Create/update YOUR member profile (role like 'backend dev', timezone, free-text about). Git identity is auto-included when available."""
    data = get_store().upsert_profile({"role": role, "timezone": timezone, "about": about})
    return f"Profile saved: {data.get('user')} ({data.get('role', '-')})."


@mcp.tool()
@guard
def get_profile(user: str = "") -> str:
    """Read a member profile (default: yours). Includes role, timezone, git identity, about."""
    import json as _json

    if not user:
        profs = get_store().all_profiles()
        if not profs:
            return "No profiles yet."
        lines = ["Team member profiles:"]
        for p in profs:
            lines.append(
                f"- {p.get('user')}: role={p.get('role', '-')} tz={p.get('timezone', '-')} email={p.get('email', '-')}"
                + (f" — {p['about']}" if p.get("about") else "")
            )
        return "\n".join(lines)
    p = get_store().get_profile(user)
    if not p:
        return f"No profile for '{user}'."
    return _json.dumps(p, indent=2, ensure_ascii=False)


# ---------------- code-aware ----------------

@mcp.tool()
@guard
def why_code(target: str) -> str:
    """'Why does this code/file exist?' Lookup across decisions/solutions/gotchas/patterns linked to a path (or mentioning it). Answers come from crew memory, ranked."""
    st = get_store()
    return _why_code_impl(st.project_git, st.all_meta_bodies, target)


@mcp.tool()
@guard
def pr_memory_review(base_ref: str = "main") -> str:
    """Review the CURRENT branch diff against base_ref through the lens of crew memory: finds memories touching changed files, flags decisions/verified knowledge likely made obsolete by this PR, so the AI can supersede/update them."""
    st = get_store()

    def all_entries():
        return [(m, b) for m, b in st.all_meta_bodies()]

    return _pr_review_impl(st.project_git, all_entries, base_ref)


@mcp.tool()
@guard
def git_blame_context(file_path: str, line_start: int, line_end: int = 0) -> str:
    """Git blame a line range in YOUR code repo, then cross-reference team memories about that file/authors — answers 'who wrote this and did anyone leave notes about it?'."""
    st = get_store()
    if not st.available_project():
        return "No code repository detected."
    blame = st.project_git.blame(file_path, line_start, line_end or line_start)
    if blame is None:
        return f"ERROR: could not blame {file_path}:{line_start}."
    out = [f"Blame {file_path} lines {line_start}-{line_end or line_start}:", "```", blame, "```"]
    related = [
        format_brief(meta, body, snippet_chars=160)
        for meta, body in st.all_meta_bodies()
        if file_path.lower() in " ".join(meta.get("files") or []).lower()
        or file_path.rsplit("/", 1)[-1].lower() in body.lower()
    ]
    if related:
        out.append("\nRelated memories:")
        out.extend(related[:5])
    return "\n".join(out)


# ---------------- prompts ----------------

@mcp.prompt()
def session_start() -> str:
    return (
        "A new session is starting. Please:\n"
        "1. Call team_context(project_path='<absolute current workspace root>') to select this repo "
        "and load who's doing what and latest project memories.\n"
        "2. Call update_status() announcing what you'll work on (ask me if unclear).\n"
        "3. Then ask me what I want to do today — using recall() before researching anything "
        "the team may already know."
    )


@mcp.prompt()
def session_end() -> str:
    return (
        "I'm ending this session. Before we stop:\n"
        "1. Summarize what was accomplished and write save_handoff(summary, next_steps, blockers, "
        "open_questions).\n"
        "2. Save any durable learnings from today that aren't in memory yet (log_solution/"
        "log_decision/save_gotcha/save_pattern).\n"
        "3. Verify any memory you proved wrong/disproved gets mark_superseded or flag_stale.\n"
        "4. Leave my status updated via update_status so teammates know where things stand."
    )


@mcp.prompt()
def pr_review_flow(base_branch: str = "main") -> str:
    return (
        f"I'm preparing a PR against '{base_branch}'. Please run pr_memory_review('{base_branch}') "
        "and walk me through: which existing decisions/memories my changes may invalidate, what "
        "should be marked superseded vs re-verified, and whether any of the changed areas lack "
        "documentation worth saving (log_decision/save_note) after merge."
    )


# ---------------- entrypoints ----------------

def run_check() -> int:
    try:
        cfg = load_config()
        store = Store(cfg)
        counts = store.counts()
        print("OK")
        print(f"  repo:   {cfg.repo_url}")
        print(f"  local:  {store.path}")
        print(f"  branch: {store.branch}")
        print(f"  user:   {cfg.user}")
        print(
            f"  entries:{counts.get('note', 0)} notes, {counts.get('decision', 0)} decisions, "
            f"{counts.get('solution', 0)} solutions, {sum(counts.values())} total"
        )
        proj = discover_project(cfg.project_path)
        print(f"  project: {proj.slug + ' @ ' + proj.branch if proj and proj.available else '(none detected)'}")
        return 0
    except (ConfigError, StoreError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


def main_run() -> None:
    mcp.run()


def main() -> None:
    parser = argparse.ArgumentParser(prog="crewmemory-server", description="Crew Memory MCP server (stdio)")
    parser.add_argument("--check", action="store_true", help="verify config + connectivity and exit")
    args = parser.parse_args()
    if args.check:
        sys.exit(run_check())
    mcp.run()


if __name__ == "__main__":
    main()
