from __future__ import annotations

import difflib

from .models import age_days, confidence, format_brief, title_similarity


def _kw_score(meta: dict, body: str, terms: list[str]) -> int:
    title = (meta.get("title") or "").lower()
    tagstr = " ".join(meta.get("tags") or []).lower()
    blob = body.lower()
    return sum(3 * title.count(t) + 2 * tagstr.count(t) + blob.count(t) for t in terms)


def rank_entries(
    entries: list[tuple[dict, str]],
    query: str,
    branch: str = "",
    changed_resolver=None,
) -> list[tuple[float, dict, str]]:
    terms = [t for t in query.lower().split() if t]
    resolve = changed_resolver or (lambda meta: set())
    ranked = []
    for meta, body in entries:
        if meta.get("status") == "superseded":
            continue
        kw = _kw_score(meta, body, terms)
        if kw <= 0:
            continue
        days = age_days(meta.get("created", ""))
        recency = pow(0.5, days / 21.0)
        conf = confidence(meta, resolve(meta))
        boost = 1.0
        if branch and meta.get("branch") == branch:
            boost += 0.3
        score = (kw * 2.0) * recency * (0.4 + conf) * boost
        ranked.append((round(score, 3), meta, body))
    ranked.sort(key=lambda r: -r[0])
    return ranked


def recall_pack(
    entries: list[tuple[dict, str]],
    query: str,
    budget_chars: int = 4000,
    branch: str = "",
    changed_resolver=None,
    limit: int = 12,
) -> tuple[str, int]:
    ranked = rank_entries(entries, query, branch, changed_resolver)
    if not ranked:
        return "", 0
    lines = []
    used = 0

    def conf_of(meta):
        return confidence(meta, (changed_resolver or (lambda m: set()))(meta))

    for i, (score, meta, body) in enumerate(ranked[:limit]):
        snippet_chars = 220 if len(ranked) > 3 else 400
        block = format_brief(meta, body, snippet_chars=snippet_chars, conf=conf_of(meta))
        if i == 0 and len(block) > budget_chars:
            block = block[:budget_chars]
        if used + len(block) + 2 > budget_chars and lines:
            break
        lines.append(f"(relevance {score}) " + block)
        used += len(block) + 2
    header = (
        f"Recall: top {len(lines)} of {len(ranked)} matches "
        f"({used}/{budget_chars} chars of context budget used). Higher relevance = better fit; "
        "conf is a decayed trust score (verified+recent=high). Use get_memory(id) for full text.\n\n"
    )
    return header + "\n\n".join(lines), used


def find_duplicates(entries: list[tuple[dict, str]], threshold: float = 0.8) -> str:
    groups = []
    seen_pairs: set[tuple[str, str]] = set()
    items = [(m, b) for m, b in entries if m.get("status") != "superseded"]
    for i, (a, _) in enumerate(items):
        for b_meta, _ in items[i + 1 :]:
            pair = tuple(sorted([a["id"], b_meta["id"]]))
            if pair in seen_pairs:
                continue
            sim = title_similarity(a.get("title", ""), b_meta.get("title", ""))
            same_files = bool(set(a.get("files") or []) & set(b_meta.get("files") or []))
            same_kind = a.get("type") == b_meta.get("type")
            reason = None
            if sim >= threshold:
                reason = f"titles {int(sim*100)}% similar"
            elif same_files and same_kind and sim >= 0.5:
                reason = "same linked files, related titles"
            if reason:
                groups.append(
                    f"- {a['id']} ('{a.get('title')}')  <->  {b_meta['id']} ('{b_meta.get('title')}')  [{reason}]"
                )
                seen_pairs.add(pair)
    if not groups:
        return "No duplicate/overlapping memories detected."
    return (
        f"{len(groups)} potential duplicate group(s) found. Review each pair: keep one, "
        "merge content into it, then mark_superseded(the other).\n" + "\n".join(groups)
    )


def pr_memory_review(
    project_git,
    store_all_entries,
    base_ref: str,
) -> str:
    if not project_git.available:
        return (
            "PR review needs access to your code repository (a git repo at the working directory "
            "or CREWMEMORY_PROJECT_PATH)."
        )
    changed = project_git.changed_between(base_ref, "HEAD")
    if changed is None:
        return f"ERROR: could not diff '{base_ref}...HEAD'. Does base branch '{base_ref}' exist?"
    if not changed:
        return f"No file differences between {base_ref} and HEAD — nothing to review."
    changed_set = set(changed)
    basenames = {c.rsplit("/", 1)[-1].lower() for c in changed}

    affected, obsolete, decisions = [], [], []
    for meta, body in store_all_entries():
        if meta.get("status") == "superseded":
            continue
        files = set(meta.get("files") or [])
        direct = files & changed_set
        mention = any(bn in body.lower() for bn in basenames if "." in bn)
        if not direct and not mention:
            continue
        line = f"- [{meta.get('type')}] {meta.get('id')} '{meta.get('title')}'"
        if direct:
            line += f" | touches: {', '.join(sorted(direct)[:3])}"
        else:
            line += " | mentions changed file(s)"
        kind = meta.get("type")
        if meta.get("status") == "verified" or kind == "decision":
            obsolete.append(line)
        elif kind in ("solution", "gotcha", "pattern"):
            affected.append(line)
        else:
            decisions.append(line)

    out = [
        f"PR review vs base '{base_ref}': {len(changed)} file(s) changed.",
        f"Changed files: {', '.join(changed[:15])}{'…' if len(changed) > 15 else ''}",
        "",
    ]
    if obsolete:
        out.append("LIKELY OBSOLETE / RE-CHECK AFTER THIS PR (decisions & verified memories touching changed code):")
        out.extend(obsolete)
        out.append("If this PR invalidates them, call mark_superseded(old_id) or verify_memory(id) after checking.")
        out.append("")
    if affected:
        out.append("AFFECTED practical knowledge (solutions/gotchas/patterns on changed files):")
        out.extend(affected)
        out.append("")
    if decisions:
        out.append("RELATED notes:")
        out.extend(decisions)
        out.append("")
    if not (obsolete or affected or decisions):
        out.append("No existing memories reference the changed files.")
    return "\n".join(out)


def why_code(project_git, all_entries, target: str, limit: int = 8) -> str:
    t = target.lower().strip("/")
    base = t.rsplit("/", 1)[-1]
    hits = []
    for meta, body in all_entries():
        if meta.get("status") == "superseded":
            continue
        files_l = [f.lower() for f in (meta.get("files") or [])]
        score = 0
        if any(f == t or f.startswith(t) or f.endswith("/" + t) for f in files_l):
            score += 10
        elif any(base and base in f for f in files_l):
            score += 6
        blob = body.lower()
        if t and t in blob:
            score += 3
        if base and base in blob:
            score += 2
        if score:
            kind_bonus = {"decision": 2, "gotcha": 1, "pattern": 1}.get(meta.get("type", ""), 0)
            hits.append((score + kind_bonus, meta, body))
    if not hits:
        return (
            f"No memories explain '{target}'. If you just learn why it exists, log_decision(...) with "
            "files=[...] so the next person finds it."
        )
    hits.sort(key=lambda h: -h[0])
    lines = [f"Why does '{target}' exist? Top explanations:"]
    for _, meta, body in hits[:limit]:
        lines.append(format_brief(meta, body, snippet_chars=300))
    return "\n\n".join(lines)
