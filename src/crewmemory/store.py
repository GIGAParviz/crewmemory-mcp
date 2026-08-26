from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, authenticated_url, redact
from .models import (
    KINDS,
    KIND_DIRS,
    Entry,
    confidence,
    dump_entry_md,
    format_brief,
    format_full,
    load_entry_md,
    make_id,
    parse_ts,
    relative_time,
    title_similarity,
    utc_now_iso,
)
from .project import ProjectGit, ProjectRepo, discover as discover_project

STATUS_STALE_SECONDS = 3 * 3600


class StoreError(Exception):
    pass


def _member_filename(user: str) -> str:
    """Return a safe stable filename for a member-controlled identifier."""
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in user).strip(".")
    return safe or "member"


class Store:
    def __init__(self, cfg: Config, project: ProjectRepo | None = None):
        self.cfg = cfg
        self.scope = "team"
        self.path: Path = cfg.local_path or (Path.home() / ".crewmemory" / cfg.repo_name)
        self.branch: str | None = None
        self.git_enabled = True
        self.project_git = ProjectGit(project or discover_project(cfg.project_path))
        self._ensure_repo()

    # ---------- construction ----------

    @classmethod
    def personal(cls, cfg: Config, project_path: Path | None = None) -> "Store":
        slug = ""
        proj = discover_project(project_path or cfg.project_path)
        if proj:
            slug = proj.slug
        store = cls.__new__(cls)
        store.cfg = cfg
        store.scope = "personal"
        store.git_enabled = False
        store.branch = None
        base = cfg.personal_root / (slug or "default")
        store.path = base
        store.project_git = ProjectGit(proj)
        store._ensure_personal()
        return store

    def set_project(self, project_path: str | Path | None = None) -> ProjectRepo | None:
        hint = Path(project_path).expanduser() if project_path else self.cfg.project_path
        project = discover_project(hint)
        self.project_git = ProjectGit(project)
        return project

    def _git_env(self) -> dict:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _git(self, *args: str, timeout: int = 180) -> subprocess.CompletedProcess:
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.path), *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._git_env(),
            )
        except FileNotFoundError:
            raise StoreError("git is not installed or not on PATH.")
        except subprocess.TimeoutExpired:
            raise StoreError(f"git {args[0]} timed out. Check network/VPN and retry.")
        if proc.returncode != 0:
            proc.stderr = redact(proc.stderr, self.cfg.token)
            proc.stdout = redact(proc.stdout, self.cfg.token)
        return proc

    def _must(self, proc: subprocess.CompletedProcess, action: str) -> subprocess.CompletedProcess:
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            hint = ""
            if "Authentication failed" in detail or "403" in detail or "could not read Username" in detail:
                hint = " Check CREWMEMORY_TOKEN: it needs read/write access to the repo."
            elif "not found" in detail.lower() or "404" in detail:
                hint = " Check CREWMEMORY_REPO_URL and that the repo exists / token can see it."
            raise StoreError(f"{action} failed:{hint}\n{detail}")
        return proc

    def _same_remote(self, a: str, b: str) -> bool:
        def norm(u: str) -> tuple[str, ...]:
            u = u.strip()
            if "://" in u:
                u = u.split("://", 1)[1]
            if "@" in u.split("/", 1)[0]:
                u = u.split("@", 1)[1]
            host, _, path = u.partition("/")
            return (host.lower(), path.rstrip("/").removesuffix(".git").lower())

        return norm(a) == norm(b)

    def _ensure_repo(self) -> None:
        if self.scope == "personal":
            self._ensure_personal()
            return
        auth_url = authenticated_url(self.cfg.repo_url, self.cfg.token)
        if self.path.exists():
            if not (self.path / ".git").exists():
                raise StoreError(
                    f"Local path {self.path} exists but is not a git repository. "
                    "Remove it or set CREWMEMORY_LOCAL_PATH."
                )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                ["git", "clone", "--quiet", auth_url, str(self.path)],
                capture_output=True,
                text=True,
                timeout=300,
                env=self._git_env(),
            )
            if proc.returncode != 0:
                msg = redact((proc.stderr or "").strip(), self.cfg.token)
                raise StoreError(f"Cloning {self.cfg.repo_url} failed.\n{msg}")

        origin = self._git("remote", "get-url", "origin").stdout.strip()
        if not self._same_remote(origin, self.cfg.repo_url):
            raise StoreError(
                f"Local repo at {self.path} points to a different remote ({redact(origin, self.cfg.token)}). "
                "Set CREWMEMORY_LOCAL_PATH to a fresh directory or fix the remote."
            )

        self._set_identity()
        self._scaffold()
        self.branch = self._current_branch()

    def _ensure_personal(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        readme = self.path / "README.md"
        if not readme.exists():
            readme.write_text(
                "# Personal Memory\n\n"
                "Private per-developer memory. Lives only on this machine; never pushed anywhere.\n",
                encoding="utf-8",
            )
        for d in (*KIND_DIRS.values(), "status", "activity"):
            (self.path / d).mkdir(exist_ok=True)

    def _set_identity(self) -> None:
        have_name = self._git("config", "user.name")
        have_email = self._git("config", "user.email")
        if have_name.returncode != 0 or not have_name.stdout.strip():
            self._git("config", "user.name", self.cfg.user)
        if have_email.returncode != 0 or not have_email.stdout.strip():
            email = self.cfg.email or f"{self.cfg.user}@users.noreply.crewmemory.local"
            self._git("config", "user.email", email)

    def _current_branch(self) -> str:
        proc = self._must(self._git("rev-parse", "--abbrev-ref", "HEAD"), "resolve branch")
        return proc.stdout.strip()

    def _scaffold(self) -> None:
        created_dirs = False
        for d in (*KIND_DIRS.values(), "status", "activity"):
            target = self.path / d
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
                created_dirs = True
        readme = self.path / "README.md"
        if not readme.exists():
            readme.write_text(
                "# Crew Memory\n\n"
                "Shared memory for this team's AI coding agents. Maintained automatically "
                "by crewmemory-mcp; edits by hand are fine too.\n\n"
                "- `notes/` facts, conventions, environment quirks\n"
                "- `decisions/` architecture & tooling decisions with rationale\n"
                "- `solutions/` problems teammates hit and how they were fixed\n"
                "- `gotchas/` small traps that cost time\n"
                "- `patterns/` recurring codebase patterns to follow\n"
                "- `handoffs/` session handoff documents between developers\n"
                "- `status/` what each member is currently working on\n"
                "- `activity/` append-only activity log per member\n"
                "- `profiles/` member profiles\n",
                encoding="utf-8",
            )
            created_dirs = True

        unborn = self._git("rev-parse", "--verify", "HEAD").returncode != 0
        dirty = self._git("status", "--porcelain").stdout.strip() != ""
        if unborn:
            self._git("add", "-A")
            self._must(self._git("commit", "-m", "[scaffold] init crew memory structure"), "commit")
            self._must(self._git("push", "-u", "origin", "HEAD"), "initial push")
        elif dirty:
            self._push_all("[scaffold] sync crew memory structure")

    # ---------- sync ----------

    def _pull(self) -> None:
        if not self.git_enabled:
            return
        proc = self._git("pull", "--rebase", "--autostash")
        if proc.returncode != 0:
            proc = self._git("pull", "--rebase", "--autostash", "origin", self.branch or "HEAD")
        self._must(proc, "sync from GitHub (pull)")

    def _push_all(self, message: str) -> None:
        if not self.git_enabled:
            return
        self._pull()
        self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet").returncode == 0:
            return
        self._must(self._git("commit", "-m", message), "commit")
        self._push_with_retry(message)

    def _push_with_retry(self, message: str, attempts: int = 3) -> None:
        last_err = ""
        for _ in range(attempts):
            proc = self._git("push", "origin", "HEAD")
            if proc.returncode == 0:
                return
            last_err = redact((proc.stderr or "") + (proc.stdout or ""), self.cfg.token)
            try:
                self._pull()
            except StoreError:
                break
        raise StoreError(
            "Pushing to GitHub failed after retries. Your change IS saved locally and committed — "
            f"run 'crewmemory doctor' or retry later (offline?).\n{last_err.strip()}"
        )

    def _sync_read(self) -> None:
        if not self.git_enabled:
            return
        try:
            self._pull()
        except StoreError as exc:
            print(
                f"[crewmemory] warning: could not sync latest ({exc}); using local copy.",
                file=sys.stderr,
            )

    def sync_memory(self, direction: str = "both") -> str:
        if self.scope == "personal":
            return "Personal memory is local-only by design — nothing to sync."
        if direction not in ("pull", "push", "both"):
            return "ERROR: direction must be 'pull', 'push', or 'both'."
        results = []
        if direction in ("pull", "both"):
            self._pull()
            results.append("pulled latest from GitHub")
        if direction in ("push", "both"):
            dirty = self._git("status", "--porcelain").stdout.strip() != ""
            ahead = self._git("rev-list", "--count", f"@{{upstream}}..HEAD")
            pending = dirty or (ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"))
            if pending:
                # A dirty worktree cannot be sent by git push alone. Commit it
                # first so offline writes are actually synchronized.
                self._push_all("[sync] manual sync of pending memory changes")
                results.append("pushed pending changes")
            else:
                results.append("nothing to push")
        return f"Sync OK ({self.scope}): " + "; ".join(results) + "."

    # ---------- entries ----------

    def _kind_dir(self, kind: str) -> Path:
        return self.path / KIND_DIRS.get(kind, kind)

    def _iter_entries(self, kinds: list[str] | None = None):
        for kind in kinds or list(KINDS):
            d = self._kind_dir(kind)
            if not d.exists():
                continue
            for f in sorted(d.glob("*.md"), reverse=True):
                meta, body = load_entry_md(f.read_text(encoding="utf-8"), kind_hint=kind)
                if not meta.get("id"):
                    continue
                yield meta, body

    def _find_entry_file(self, entry_id: str) -> tuple[Path, dict, str] | None:
        matches = []
        for kind in KINDS:
            d = self._kind_dir(kind)
            if not d.exists():
                continue
            for f in d.glob("*.md"):
                if f.stem == entry_id or f.stem.startswith(entry_id):
                    meta, body = load_entry_md(f.read_text(encoding="utf-8"), kind_hint=kind)
                    matches.append((f, meta, body))
        if not matches:
            return None
        exact = [m for m in matches if m[0].stem == entry_id]
        return (exact or matches)[0]

    def _dedupe_warnings(self, kind: str, title: str, tags: list[str], files: list[str]) -> list[str]:
        warns = []
        tagset = {t.lower() for t in tags}
        fileset = set(files or [])
        for meta, _ in self._iter_entries([kind]):
            if meta.get("superseded_by"):
                continue
            sim = title_similarity(title, meta.get("title", ""))
            same_files = bool(fileset and fileset & set(meta.get("files") or []))
            same_tags = bool(tagset and tagset & {t.lower() for t in (meta.get("tags") or [])})
            if sim >= 0.82 or (same_files and same_tags):
                rel = (
                    "near-identical title"
                    if sim >= 0.82
                    else "overlapping files+tags"
                )
                warns.append(
                    f"possible duplicate/conflict with {meta['id']} ('{meta.get('title')}') — {rel}. "
                    "If yours replaces it, call mark_superseded(old_id, new_id)."
                )
        return warns[:2]

    def save_entry(
        self,
        kind: str,
        title: str,
        body: str,
        tags: list[str],
        files: list[str] | None = None,
        commits: list[str] | None = None,
        extra: dict | None = None,
        dedupe_check: bool = True,
    ) -> tuple[Entry, list[str]]:
        now = utc_now_iso()
        ex = dict(extra or {})
        ex.setdefault("status", "unverified")
        snap = self.project_git.snapshot_meta()
        for k in ("project", "branch"):
            if snap.get(k) and k not in ex:
                ex[k] = snap[k]
        if files:
            ex["files"] = [f.strip() for f in files if f.strip()]
        if commits:
            ex["commits"] = [c.strip() for c in commits if c.strip()]

        warns = self._dedupe_warnings(kind, title, tags, files) if dedupe_check else []

        entry = Entry(
            id=make_id(self.cfg.user),
            kind=kind,
            title=title.strip(),
            author=self.cfg.user,
            created=now,
            tags=[t.strip() for t in (tags or []) if t.strip()],
            body=body.strip(),
            scope=self.scope,
            extra=ex,
        )
        path = self._kind_dir(kind) / f"{entry.id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_entry_md(entry), encoding="utf-8")
        self._log_activity(kind, entry.title)
        self._push_all(f"[{kind}] {self.cfg.user}: {entry.title}")
        return entry, warns

    def get_entry(self, entry_id: str, changed_files: set[str] | None = None) -> str:
        self._sync_read()
        found = self._find_entry_file(entry_id)
        if not found:
            return f"ERROR: no memory entry found matching id '{entry_id}'. Use search_memory or list_recent to find ids."
        path, meta, body = found
        text = format_full(meta, body)
        conf = confidence(meta, changed_files)
        return text + f"\n\nconfidence: {conf} | status: {meta.get('status', 'unverified')}{(' -> ' + meta['superseded_by']) if meta.get('superseded_by') else ''}"

    def delete_entry(self, entry_id: str) -> str:
        self._sync_read()
        found = self._find_entry_file(entry_id)
        if not found:
            return f"ERROR: no entry matching '{entry_id}'."
        path, meta, body = found
        author = meta.get("author", "?")
        if author != self.cfg.user:
            return (
                f"ERROR: '{path.stem}' was saved by '{author}'. Only the author deletes their entries "
                f"(you are '{self.cfg.user}'). Prefer mark_superseded instead."
            )
        title = meta.get("title", path.stem)
        path.unlink()
        self._log_activity("deleted", title)
        self._push_all(f"[delete] {self.cfg.user}: {title}")
        return f"Deleted entry '{title}' ({path.stem}) and pushed to GitHub."

    def set_lifecycle(self, entry_id: str, new_status: str, superseded_by: str = "") -> str:
        self._sync_read()
        found = self._find_entry_file(entry_id)
        if not found:
            return f"ERROR: no entry matching '{entry_id}'."
        path, meta, body = found
        old = meta.get("status", "unverified")
        meta["status"] = new_status
        meta["updated"] = utc_now_iso()
        if superseded_by:
            meta["superseded_by"] = superseded_by
        elif new_status != "superseded":
            meta.pop("superseded_by", None)
        entry = Entry(
            id=meta["id"],
            kind=meta.get("type", "note"),
            title=meta.get("title", ""),
            author=meta.get("author", "?"),
            created=meta.get("created", ""),
            tags=meta.get("tags") or [],
            body=body,
            scope=self.scope,
            extra={k: v for k, v in meta.items() if k not in ("id", "type", "title", "author", "created", "tags")},
        )
        path.write_text(dump_entry_md(entry), encoding="utf-8")
        verb = {"verified": "verified", "stale": "flagged stale", "unverified": "reset", "superseded": "marked superseded"}[new_status]
        self._log_activity("lifecycle", f"{verb}: {meta.get('title')}")
        self._push_all(f"[lifecycle] {self.cfg.user}: {verb} '{meta.get('title')}' ({old}->{new_status})")
        tail = f" (superseded_by={superseded_by})" if superseded_by else ""
        return f"'{meta.get('title')}' is now {new_status}{tail}. Pushed."

    # ---------- search / list ----------

    def search(
        self,
        query: str,
        kind: str | None = None,
        tags: list[str] | None = None,
        author: str | None = None,
        file: str | None = None,
        project: str | None = "__current__",
        limit: int = 10,
    ) -> str:
        self._sync_read()
        terms = query.lower().split()
        if project == "__current__":
            project = self.project_git.repo.slug if self.available_project() else None
        kinds = [kind] if kind in KINDS else list(KINDS)
        want_tags = {t.lower() for t in (tags or [])}
        scored: list[tuple[int, str, str]] = []
        for meta, body in self._iter_entries(kinds):
            if author and (meta.get("author") or "").lower() != author.lower():
                continue
            if project and (meta.get("project") or "") != project:
                continue
            mtags = {t.lower() for t in (meta.get("tags") or [])}
            if want_tags and not (want_tags & mtags):
                continue
            mfiles = [f.lower() for f in (meta.get("files") or [])]
            if file and file.lower() not in " ".join(mfiles) and file.lower() not in body.lower():
                continue
            title = (meta.get("title") or "").lower()
            tagstr = " ".join(mtags).lower()
            blob = body.lower()
            score = sum(3 * title.count(t) + 2 * tagstr.count(t) + blob.count(t) for t in terms)
            if terms and score <= 0:
                continue
            scored.append((max(score, 1), meta.get("created", ""), format_brief(meta, body)))
        if not scored:
            filters = ", ".join(x for x in [f"kind={kind}" if kind else "", f"author={author}" if author else "", f"tags={tags}" if tags else "", f"file={file}" if file else "", f"project={project}" if project else ""] if x)
            return f"No memories match '{query}'" + (f" ({filters})" if filters else "") + ". Try broader terms or list_recent."
        created_desc = sorted({s[1] for s in scored}, reverse=True)
        order = {c: i for i, c in enumerate(created_desc)}
        scored.sort(key=lambda s: (-s[0], order[s[1]]))
        lines = [s[2] for s in scored[: max(1, limit)]]
        return f"Found {len(scored)} match(es); showing {len(lines)}:\n\n" + "\n\n".join(lines)

    def available_project(self) -> bool:
        return self.project_git.available

    def list_recent(self, kind: str | None = None, limit: int = 15, project: str | None = "__all__") -> str:
        self._sync_read()
        kinds = [kind] if kind in KINDS else list(KINDS)
        items: list[tuple[str, str]] = []
        for meta, body in self._iter_entries(kinds):
            if project != "__all__" and (meta.get("project") or "") != project:
                continue
            items.append((meta.get("created", ""), format_brief(meta, body)))
        if not items:
            scope = f"type '{kind}'" if kind else "any type"
            return f"No memories yet ({scope}). Save some with save_note / log_decision / log_solution."
        items.sort(key=lambda x: x[0], reverse=True)
        shown = items[: max(1, limit)]
        return f"{len(items)} total; most recent:\n\n" + "\n\n".join(s for _, s in shown)

    def stats(self) -> str:
        self._sync_read()
        by_kind: dict[str, int] = {}
        by_author: dict[str, int] = {}
        by_tag: dict[str, int] = {}
        by_project: dict[str, int] = {}
        by_status: dict[str, int] = {}
        total = 0
        for meta, _ in self._iter_entries(None):
            total += 1
            by_kind[meta.get("type", "?")] = by_kind.get(meta.get("type", "?"), 0) + 1
            by_author[meta.get("author", "?")] = by_author.get(meta.get("author", "?"), 0) + 1
            by_status[meta.get("status", "unverified")] = by_status.get(meta.get("status", "unverified"), 0) + 1
            if meta.get("project"):
                by_project[meta["project"]] = by_project.get(meta["project"], 0) + 1
            for t in meta.get("tags") or []:
                by_tag[t] = by_tag.get(t, 0) + 1
        def fmt(d: dict, n: int = 8) -> str:
            items = sorted(d.items(), key=lambda kv: -kv[1])[:n]
            return ", ".join(f"{k}:{v}" for k, v in items) or "-"
        lines = [
            f"Memory index ({self.scope}): {total} entries",
            f"by type:     {fmt(by_kind)}",
            f"by author:   {fmt(by_author)}",
            f"by status:   {fmt(by_status)}",
            f"by project:  {fmt(by_project)}",
            f"top tags:    {fmt(by_tag, 12)}",
        ]
        return "\n".join(lines)

    # ---------- presence & activity ----------

    def update_status(self, task: str, progress: int | None = None, blockers: list[str] | None = None) -> str:
        self._sync_read()
        now = utc_now_iso()
        status_dir = self.path / "status"
        status_dir.mkdir(exist_ok=True)
        payload = {
            "user": self.cfg.user,
            "task": task.strip(),
            "progress": progress if progress is not None else None,
            "blockers": blockers or [],
            "updated": now,
        }
        if self.available_project():
            payload["project"] = self.project_git.repo.slug
            payload["branch"] = self.project_git.repo.branch
        (status_dir / f"{_member_filename(self.cfg.user)}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        summary = task.strip()[:60]
        self._log_activity("status", summary + (f" [{progress}%]" if progress is not None else ""))
        self._push_all(f"[status] {self.cfg.user}: {summary}")
        blurb = ""
        if blockers:
            blurb = f" Blockers recorded: {len(blockers)}."
        prog = f" at {progress}%" if progress is not None else ""
        return f"Status shared{prog}.{blurb}"

    def get_team_status(self) -> str:
        self._sync_read()
        status_dir = self.path / "status"
        rows: list[tuple[str, str]] = []
        if status_dir.exists():
            for f in status_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                user = data.get("user", f.stem)
                updated = data.get("updated", "?")
                rel = relative_time(updated)
                stale = ""
                dt = parse_ts(updated)
                if dt:
                    age = (datetime.now(timezone.utc) - dt).total_seconds()
                    if age > STATUS_STALE_SECONDS:
                        stale = " [stale]"
                task = data.get("task") or data.get("message") or ""
                where = f" ({data['project']}@{data['branch']})" if data.get("project") else ""
                prog = f" [{data['progress']}%]" if data.get("progress") is not None else ""
                row = f"- {user}{stale} ({rel}){where}: {task}{prog}"
                blockers = data.get("blockers") or []
                for b in blockers:
                    row += f"\n    BLOCKED: {b}"
                rows.append((updated, row))
        if not rows:
            return (
                "No one has announced a status yet. Teammates should call update_status(...) "
                "when they start work so everyone knows who is doing what."
            )
        rows.sort(key=lambda r: r[0], reverse=True)
        return "Team status (who's doing what):\n" + "\n".join(r for _, r in rows)

    def recent_activity(self, limit: int = 30, author: str | None = None) -> str:
        self._sync_read()
        events: list[tuple[str, str]] = []
        act_dir = self.path / "activity"
        if act_dir.exists():
            for f in act_dir.glob("*.jsonl"):
                if author and f.stem.lower() != author.lower():
                    continue
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = ev.get("ts", "")
                    user = ev.get("user", "?")
                    action = ev.get("action", "?")
                    detail = str(ev.get("detail", ""))[:100]
                    label = {
                        "note": "saved note",
                        "decision": "logged decision",
                        "solution": "logged solution",
                        "gotcha": "saved gotcha",
                        "pattern": "saved pattern",
                        "handoff": "wrote handoff",
                        "status": "status",
                        "deleted": "deleted",
                        "lifecycle": "lifecycle",
                    }.get(action, action)
                    events.append((ts, f"{ts}  {user:<12} {label}: {detail}"))
        if not events:
            return "No activity recorded yet."
        events.sort(key=lambda e: e[0], reverse=True)
        return "Recent team activity:\n" + "\n".join(text for _, text in events[: max(1, limit)])

    def team_context(self) -> str:
        self._sync_read()
        proj = self.project_git.repo.slug if self.available_project() else "(no project detected)"
        br = self.project_git.current_branch() or "-"
        parts = [
            f"# Crew Memory context | project: {proj} | branch: {br}",
            self.get_team_status(),
        ]
        recent = []
        current_project = self.project_git.repo.slug if self.available_project() else None
        for meta, body in self._iter_entries(None):
            if current_project and (meta.get("project") or "") not in ("", current_project):
                continue
            recent.append((meta.get("created", ""), format_brief(meta, body, snippet_chars=160)))
        recent.sort(key=lambda x: x[0], reverse=True)
        latest = "\n\n".join(s for _, s in recent[:5]) if recent else "(no memories saved yet)"
        parts.append("Latest memories:\n" + latest)
        parts.append(
            "Tips: update_status(task, progress, blockers) when you switch tasks; recall(query) is the smart "
            "retrieval tool (ranks by relevance+confidence, respects context budget); search_memory for raw "
            "searches; save durable learnings (save_note/log_decision/log_solution/save_gotcha/save_pattern); "
            "end sessions with save_handoff."
        )
        return "\n\n".join(parts)

    # ---------- history / provenance / time travel ----------

    def entry_history(self, entry_id: str) -> str:
        self._sync_read()
        found = self._find_entry_file(entry_id)
        if not found:
            return f"ERROR: no entry matching '{entry_id}'."
        path, meta, body = found
        rel = path.relative_to(self.path).as_posix()
        if not self.git_enabled:
            return f"Personal memory has no git history. Created {meta.get('created')} by {meta.get('author')}."
        log = self._git(
            "log", "--follow", "--format=%h|%an|%ad|%s", "--date=iso", "--", rel
        )
        if log.returncode != 0:
            return f"ERROR reading history: {log.stderr}"
        lines = [l for l in log.stdout.strip().splitlines() if l]
        if not lines:
            return "No git history found (file not committed yet)."
        out = [f"History of '{meta.get('title')}' ({path.stem}) — every commit that touched it:"]
        for l in lines:
            h, an, ad, s = (l.split("|", 3) + [""])[:4]
            out.append(f"  {h}  {ad[:16]}  {an:<12} {s}")
        return "\n".join(out)

    def memory_at(self, ref: str, kind: str | None = None, limit: int = 25) -> str:
        if self.scope == "personal":
            return "Time travel is only available for crew memory (needs git history)."
        self._sync_read()
        verify = self._git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        if verify.returncode != 0:
            return f"ERROR: '{ref}' is not a valid commit/tag/branch in the memory repo."
        sha = self._git("rev-parse", "--short", ref + "^{commit}").stdout.strip()
        ls = self._git("ls-tree", "-r", "--name-only", ref)
        files_now = {f.relative_to(self.path).as_posix() for f in self.path.rglob("*.md")}
        old_entries = []
        kinds_filter = [kind] if kind in KINDS else list(KINDS)
        prefixes = tuple(KIND_DIRS[k] + "/" for k in kinds_filter)
        count_at_ref = 0
        for line in (ls.stdout or "").splitlines():
            if line.startswith(prefixes) and line.endswith(".md"):
                count_at_ref += 1
                show = self._git("show", f"{ref}:{line}")
                if show.returncode != 0:
                    continue
                meta, body = load_entry_md(show.stdout, kind_hint="note")
                if meta.get("id"):
                    old_entries.append((meta.get("created", ""), format_brief(meta, body, snippet_chars=140)))
        old_entries.sort(key=lambda x: x[0], reverse=True)
        shown = old_entries[: max(1, limit)]
        current_total = len(files_now)
        head = (
            f"Crew memory as of {ref} (commit {sha}): {count_at_ref} entries "
            f"(today: {current_total}, so {current_total - count_at_ref} newer)."
        )
        listing = "\n\n".join(s for _, s in shown) if shown else "(memory was empty at this ref)"
        return head + "\n\n" + listing

    # ---------- stats helper ----------

    def counts(self) -> dict[str, int]:
        out = {}
        for kind in KINDS:
            d = self._kind_dir(kind)
            out[kind] = len(list(d.glob("*.md"))) if d.exists() else 0
        return out

    def all_meta_bodies(self):
        return [(dict(meta, scope=self.scope), body) for meta, body in self._iter_entries(None)]

    def snapshot(self) -> dict:
        """JSON-ready view of everything in this store (for the human dashboard)."""
        self._sync_read()
        entries = []
        for meta, body in self.all_meta_bodies():
            entries.append(
                {
                    "id": meta.get("id", ""),
                    "type": meta.get("type", "note"),
                    "title": meta.get("title", "(untitled)"),
                    "author": meta.get("author", "?"),
                    "created": meta.get("created", ""),
                    "updated": meta.get("updated", meta.get("created", "")),
                    "tags": meta.get("tags") or [],
                    "project": meta.get("project", ""),
                    "branch": meta.get("branch", ""),
                    "files": meta.get("files") or [],
                    "status": meta.get("status", "unverified"),
                    "superseded_by": meta.get("superseded_by", ""),
                    "scope": meta.get("scope", self.scope),
                    "confidence": confidence(meta),
                    "body": body[:4000],
                }
            )
        statuses = []
        status_dir = self.path / "status"
        if status_dir.exists():
            for f in sorted(status_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                dt = parse_ts(data.get("updated", ""))
                age = (datetime.now(timezone.utc) - dt).total_seconds() if dt else 0
                data["stale"] = bool(dt and age > STATUS_STALE_SECONDS)
                data["age_seconds"] = int(age)
                statuses.append(data)
        activity = []
        act_dir = self.path / "activity"
        if act_dir.exists():
            for f in sorted(act_dir.glob("*.jsonl")):
                for line in f.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        ev["scope"] = self.scope
                        activity.append(ev)
                    except json.JSONDecodeError:
                        continue
        activity.sort(key=lambda e: e.get("ts", ""), reverse=True)
        return {
            "scope": self.scope,
            "entries": entries,
            "statuses": statuses,
            "activity": activity[:500],
            "profiles": self.all_profiles(),
            "counts": self.counts(),
        }


    def _log_activity(self, action: str, detail: str) -> None:
        log_dir = self.path / "activity"
        log_dir.mkdir(exist_ok=True)
        line = json.dumps(
            {"ts": utc_now_iso(), "user": self.cfg.user, "action": action, "detail": detail},
            ensure_ascii=False,
        )
        with (log_dir / f"{_member_filename(self.cfg.user)}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ---------- profiles ----------

    def profiles_dir(self) -> Path:
        d = self.path / "profiles"
        d.mkdir(exist_ok=True)
        return d

    def get_profile(self, user: str | None = None) -> dict | None:
        self._sync_read()
        who = user or self.cfg.user
        p = self.profiles_dir() / f"{_member_filename(who)}.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def upsert_profile(self, updates: dict, auto_from_git: bool = False) -> dict:
        self._sync_read()
        p = self.profiles_dir() / f"{_member_filename(self.cfg.user)}.json"
        data = {}
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        data.setdefault("user", self.cfg.user)
        if auto_from_git and self.project_git.available:
            g = self.project_git.repo
            data.setdefault("git_name", g.user_name)
            data.setdefault("email", g.user_email)
        for k, v in updates.items():
            if v not in (None, "", [], {}):
                data[k] = v
        data["updated"] = utc_now_iso()
        existed_before = p.exists()
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if not existed_before:
            self._push_all(f"[profile] create profile for {self.cfg.user}")
        else:
            self._push_all(f"[profile] update profile for {self.cfg.user}")
        return data

    def all_profiles(self) -> list[dict]:
        self._sync_read()
        out = []
        for f in sorted(self.profiles_dir().glob("*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return out
