from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher

import yaml

KINDS = ("note", "decision", "solution", "gotcha", "pattern", "handoff")
KIND_DIRS = {k: f"{k}s" for k in KINDS}
LIFECYCLE = ("unverified", "verified", "stale", "superseded")


@dataclass
class Entry:
    id: str
    kind: str
    title: str
    author: str
    created: str
    tags: list[str] = field(default_factory=list)
    body: str = ""
    scope: str = "team"
    extra: dict = field(default_factory=dict)

    @property
    def status(self) -> str:
        return self.extra.get("status", "unverified")

    @property
    def project(self) -> str:
        return self.extra.get("project", "")

    @property
    def branch(self) -> str:
        return self.extra.get("branch", "")

    @property
    def files(self) -> list[str]:
        return self.extra.get("files") or []

    @property
    def commits(self) -> list[str]:
        return self.extra.get("commits") or []

    @property
    def superseded_by(self) -> str:
        return self.extra.get("superseded_by", "")

    @property
    def updated(self) -> str:
        return self.extra.get("updated", self.created)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_id(user: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    slug = "".join(c if c.isalnum() else "-" for c in user.lower())[:12].strip("-")
    return f"{ts}-{slug or 'user'}-{uuid.uuid4().hex[:6]}"


def parse_ts(iso: str) -> datetime | None:
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def age_days(iso: str) -> float:
    dt = parse_ts(iso)
    if not dt:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)


def relative_time(iso: str) -> str:
    dt = parse_ts(iso)
    if not dt:
        return iso or "?"
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def normalize_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def confidence(meta: dict, changed_files: set[str] | None = None) -> float:
    score = 1.0
    days = age_days(meta.get("created", ""))
    score -= min(days * 0.02, 0.45)
    files = set((meta.get("files") or []))
    if files and changed_files and (files & changed_files):
        score -= 0.35
    st = meta.get("status", "unverified")
    if st == "verified":
        score += 0.15
    elif st == "stale":
        score *= 0.4
    elif st == "superseded":
        return 0.05
    return round(max(0.05, min(score, 1.0)), 2)


def dump_entry_md(entry: Entry) -> str:
    meta = {
        "id": entry.id,
        "type": entry.kind,
        "title": entry.title,
        "author": entry.author,
        "created": entry.created,
        "updated": entry.updated,
        "tags": entry.tags,
    }
    for k, v in entry.extra.items():
        if v not in (None, "", [], {}):
            meta[k] = v
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{entry.body.strip()}\n"


def load_entry_md(text: str, kind_hint: str = "note") -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            try:
                meta = yaml.safe_load("\n".join(lines[1:i])) or {}
            except yaml.YAMLError:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            body = "\n".join(lines[i + 1 :]).strip()
            return meta, body
    return {}, text.strip()


def _badges(meta: dict) -> str:
    out = []
    st = meta.get("status", "unverified")
    if st == "verified":
        out.append("verified")
    elif st == "stale":
        out.append("STALE")
    if meta.get("superseded_by"):
        out.append(f"superseded->{meta['superseded_by']}")
    if meta.get("scope") == "personal":
        out.append("personal")
    return ("[" + ",".join(out) + "]") if out else ""


def format_brief(meta: dict, body: str, snippet_chars: int = 240, conf: float | None = None) -> str:
    eid = meta.get("id", "?")
    kind = meta.get("type", "note")
    title = meta.get("title", "(untitled)")
    author = meta.get("author", "?")
    created = meta.get("created", "?")
    parts = [f"[{eid}] ({kind}) {title}", f"    by {author} | {created}"]
    extras = []
    if meta.get("project"):
        extras.append(f"project:{meta['project']}")
    if meta.get("branch"):
        extras.append(f"branch:{meta['branch']}")
    if meta.get("files"):
        extras.append("files:" + ",".join(meta["files"][:4]) + ("…" if len(meta["files"]) > 4 else ""))
    tags = meta.get("tags") or []
    if tags:
        extras.append("tags:" + ",".join(tags[:6]))
    badge = _badges(meta)
    if badge:
        extras.append(badge)
    if conf is not None:
        extras.append(f"conf:{conf}")
    out = parts[0] + "\n    by " + author + " | " + created
    if extras:
        out += " | " + " | ".join(extras)
    snippet = " ".join(body.split())[:snippet_chars]
    if snippet:
        out += f"\n    {snippet}"
    return out


def format_full(meta: dict, body: str) -> str:
    keys = ("id", "type", "title", "author", "created", "updated", "tags", "project", "branch", "files", "commits", "status", "superseded_by")
    lines = []
    for k in keys:
        v = meta.get(k)
        if v in (None, "", [], {}, "unverified"):
            continue
        if isinstance(v, list):
            v = ", ".join(str(x) for x in v)
        lines.append(f"{k}: {v}")
    return "\n".join(lines) + f"\n\n{body}"
