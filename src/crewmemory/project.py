from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .models import utc_now_iso


@dataclass
class ProjectRepo:
    path: Path
    slug: str = ""
    branch: str = ""
    user_name: str = ""
    user_email: str = ""
    available: bool = True
    problems: list[str] = field(default_factory=list)


def discover(explicit: Path | None) -> ProjectRepo | None:
    candidates = []
    if explicit:
        candidates.append(explicit)
    cwd_env = os.environ.get("CREWMEMORY_PROJECT_PATH", "").strip()
    if cwd_env:
        candidates.append(Path(cwd_env).expanduser())
    candidates.append(Path.cwd())

    seen: set[Path] = set()
    for base in candidates:
        try:
            base = base.resolve()
        except OSError:
            continue
        root = _find_git_root(base)
        if not root or root in seen:
            continue
        seen.add(root)
        repo = _inspect(root)
        if repo and repo.available:
            return repo
    return None


def _find_git_root(start: Path) -> Path | None:
    cur = start
    for _ in range(10):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _git(root: Path, *args: str, timeout: int = 60):
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _inspect(root: Path) -> ProjectRepo | None:
    head = _git(root, "rev-parse", "--is-inside-work-tree")
    if not head or head.returncode != 0 or head.stdout.strip() != "true":
        return None
    repo = ProjectRepo(path=root, available=True)
    repo.slug = root.name
    remote = _git(root, "remote", "get-url", "origin")
    if remote and remote.returncode == 0 and remote.stdout.strip():
        name = Path(remote.stdout.strip().rstrip("/")).name
        if name.endswith(".git"):
            name = name[: -len(".git")]
        repo.slug = name or repo.slug
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch.returncode == 0:
        repo.branch = branch.stdout.strip()
    name = _git(root, "config", "user.name")
    email = _git(root, "config", "user.email")
    repo.user_name = name.stdout.strip() if name and name.returncode == 0 else ""
    repo.user_email = email.stdout.strip() if email and email.returncode == 0 else ""
    return repo


class ProjectGit:
    """Read-only helpers against the developer's actual code repository."""

    def __init__(self, repo: ProjectRepo | None):
        self.repo = repo

    @property
    def available(self) -> bool:
        return bool(self.repo and self.repo.available)

    def _require(self):
        if not self.available:
            return None
        return self.repo.path

    def current_branch(self) -> str:
        return self.repo.branch if self.available else ""

    def changed_files_since(self, iso_ts: str) -> set[str]:
        root = self._require()
        if not root:
            return set()
        rev = _git(root, "rev-list", "-1", f"--before={iso_ts}", "HEAD")
        if not rev or rev.returncode != 0 or not rev.stdout.strip():
            return set()
        base = rev.stdout.strip()
        diff = _git(root, "diff", "--name-only", f"{base}..HEAD")
        if not diff or diff.returncode != 0:
            return set()
        return {l.strip() for l in diff.stdout.splitlines() if l.strip()}

    def changed_between(self, base_ref: str, head_ref: str = "HEAD") -> list[str] | None:
        root = self._require()
        if not root:
            return None
        diff = _git(root, "diff", "--name-only", f"{base_ref}...{head_ref}")
        if not diff or diff.returncode != 0:
            return None
        return [l.strip() for l in diff.stdout.splitlines() if l.strip()]

    def commit_log(self, base_ref: str, head_ref: str = "HEAD", limit: int = 40) -> str | None:
        root = self._require()
        if not root:
            return None
        log = _git(
            root,
            "log",
            f"--max-count={limit}",
            "--format=%h %an %ad%n    %s%n",
            "--date=short",
            f"{base_ref}..{head_ref}",
        )
        if not log or log.returncode != 0:
            return None
        stat = _git(root, "diff", "--stat", f"{base_ref}...{head_ref}")
        out = log.stdout.strip() or "(no commits)"
        if stat and stat.returncode == 0 and stat.stdout.strip():
            out += "\n\nFiles touched:\n" + "\n".join(stat.stdout.strip().splitlines()[-25:])
        return out

    def blame(self, file: str, line_start: int, line_end: int | None = None) -> str | None:
        root = self._require()
        if not root:
            return None
        end = line_end or line_start
        bl = _git(root, "blame", "-L", f"{line_start},{end}", "--date=short", "--", file)
        if not bl or bl.returncode != 0:
            return None
        return bl.stdout.strip()

    def recent_commit_of_file(self, file: str) -> tuple[str, str] | None:
        root = self._require()
        if not root:
            return None
        log = _git(root, "log", "-1", "--format=%h|%ad|%s", "--date=short", "--", file)
        if not log or log.returncode != 0 or not log.stdout.strip():
            return None
        parts = log.stdout.strip().split("|", 2)
        if len(parts) == 3:
            return parts[0], f"{parts[1]} {parts[2]}"
        return None

    def file_exists_in_head(self, file: str) -> bool:
        root = self._require()
        if not root:
            return False
        proc = _git(root, "cat-file", "-e", f"HEAD:{file}", "--")
        return bool(proc and proc.returncode == 0)

    def suggest_project_slug(self) -> str:
        return self.repo.slug if self.available else ""

    def snapshot_meta(self) -> dict:
        if not self.available:
            return {}
        meta = {"project": self.repo.slug}
        if self.repo.branch:
            meta["branch"] = self.repo.branch
        return meta


def empty_project() -> ProjectRepo:
    return ProjectRepo(path=Path(os.getcwd()), available=False)
