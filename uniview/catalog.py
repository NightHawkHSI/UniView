"""The projects page's logic: which saved games a search shows and how they're grouped. No Qt here."""

import os

import engines
from uniview.projects import engine_group, engine_info_text, version_key
from uniview.settings import COMPAT_STATUS

UNTAGGED = "__untagged__"               # tag filter: games without tags
UNKNOWN_ENGINE = "__unknown_engine__"   # engine filter: games no plugin recognized


def compat_entry(compat, path):
    """The compatibility list entry for a game folder, or None."""
    return compat.get(os.path.basename(os.path.normpath(path)).lower())


def project_groups(project, group_by, compat, is_loaded):
    """[(sort key, group title)] a game belongs to (a game with several tags is in several groups)."""
    if group_by == "tag":
        return [((0, t.lower()), f"#{t}") for t in project.get("tags", [])] or [((1,), "Untagged")]
    if group_by == "engine":
        plugin = engines.get(project.get("engine") or "")
        return [((0, plugin.name.lower()), plugin.name)] if plugin else [((1,), "Unknown engine")]
    if group_by == "version":
        title = engine_group(project)
        if title == "Unknown engine":
            return [((1,), title)]
        return [((0, project.get("engine") or "", tuple(-n for n in version_key(title))), title)]
    if group_by == "status":
        status = (compat_entry(compat, project["path"]) or {}).get("status")
        order = list(COMPAT_STATUS)
        if status in COMPAT_STATUS:
            return [((order.index(status),), COMPAT_STATUS[status])]
        return [((len(order),), "Not rated yet")]
    if group_by == "state":
        if not os.path.isdir(project["path"]):
            return [((2,), "Folder missing")]
        if is_loaded(project["path"]):
            return [((0,), "Loaded")]
        return [((1,), "Not loaded")]
    return [((0,), "")]


def project_matches(project, terms, compat):
    """Plain words match anywhere (name, tags, notes, engine...); tag:/engine:/version:/backend:/status: narrow it."""
    entry = compat_entry(compat, project["path"]) or {}
    tags = [t.lower() for t in project.get("tags", [])]
    fields = {
        "tag": tags,
        "engine": [(project.get("engine") or "").lower(),
                   (getattr(engines.get(project.get("engine") or ""), "name", "") or "").lower()],
        "version": [(project.get("engine_version") or "").lower()],
        "unity": [(project.get("engine_version") or "").lower()] if project.get("engine") == "unity" else [],
        "backend": [w.strip(",") for w in (project.get("engine_detail") or "").lower().split()],
        "status": [(entry.get("status") or "").lower()],
    }
    haystack = " ".join([project["name"], os.path.basename(os.path.normpath(project["path"])),
                         project.get("notes", ""), engine_info_text(project), entry.get("status", ""),
                         " ".join("#" + t for t in tags)]).lower()
    for term in terms:
        key, sep, value = term.partition(":")
        if sep and key in fields:
            if not any(v.startswith(value) for v in fields[key] if v):
                return False
        elif term not in haystack:
            return False
    return True


def project_shown(project, tag, engine_filter, terms, compat):
    """Does the game pass the tag / engine drop-downs and the search box?"""
    if tag == UNTAGGED and project.get("tags"):
        return False
    if tag and tag != UNTAGGED and tag not in project.get("tags", []):
        return False
    if engine_filter and (project.get("engine") or UNKNOWN_ENGINE) != engine_filter:
        return False
    return project_matches(project, terms, compat)


def group_projects(projects, group_by, compat, is_loaded):
    """[(title, [projects])] in display order; a game can be in several groups."""
    groups = {}  # title -> (sort key, [projects])
    for project in projects:
        for order, title in project_groups(project, group_by, compat, is_loaded):
            groups.setdefault(title, (order, []))[1].append(project)
    return [(title, items) for title, (_order, items) in sorted(groups.items(), key=lambda kv: (kv[1][0], kv[0].lower()))]
