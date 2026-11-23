"""Directory tree for the settings UI.

The fnOS database stores an absolute file path for every episode. Grouping
those paths by directory gives a faithful view of the media library without
needing the media directories to be mounted into the container.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from typing import Any

from .models import SeriesRef


def normalise_dir(path: str | None) -> str:
    """Return a clean absolute POSIX directory, or '' when there is none."""
    if not path:
        return ""
    text = str(path).strip().replace("\\", "/")
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    text = posixpath.dirname(text) if not text.endswith("/") else text.rstrip("/")
    if not text or text == "/":
        return ""
    return posixpath.normpath(text)


def build_folder_tree(series: Iterable[SeriesRef]) -> dict[str, Any]:
    """Aggregate episode paths into a collapsed directory tree.

    Chains of single-child directories are merged so the tree starts at the
    library folder (``/vol1/1000/影片/动漫``) instead of ``/`` -> ``vol1`` ...
    """
    root: dict[str, Any] = {"path": "", "name": "", "direct": 0, "episodes": 0, "children": {}}

    for item in series:
        directory = normalise_dir(item.library_path)
        if not directory:
            continue
        node = root
        current = ""
        for part in (piece for piece in directory.split("/") if piece):
            current += "/" + part
            child = node["children"].get(part)
            if child is None:
                child = {"path": current, "name": part, "direct": 0, "episodes": 0, "children": {}}
                node["children"][part] = child
            node = child
        node["direct"] += 1
        node["episodes"] += max(0, int(item.episode_count or 0))

    def aggregate(node: dict[str, Any]) -> dict[str, Any]:
        children = [aggregate(child) for child in node["children"].values()]
        return {
            "path": node["path"],
            "name": node["name"],
            "direct": node["direct"],
            "episodes": node["episodes"] + sum(child["episodes"] for child in children),
            "series": node["direct"] + sum(child["series"] for child in children),
            "children": children,
        }

    def collapse(node: dict[str, Any]) -> dict[str, Any]:
        children = [collapse(child) for child in node["children"]]
        if len(children) == 1 and node["direct"] == 0:
            # merge "/vol1" -> "/1000" -> "/影片" -> "/动漫" into one node
            return children[0]
        return {**node, "children": children}

    tree = aggregate(root)
    roots = sorted(
        (collapse(child) for child in tree["children"]),
        key=lambda node: (-node["series"], node["name"]),
    )

    def public(node: dict[str, Any]) -> dict[str, Any]:
        return {
            "path": node["path"],
            "name": node["name"],
            "series": node["series"],
            "episodes": node["episodes"],
            "children": [public(child) for child in node["children"]],
        }

    return {
        "roots": [public(node) for node in roots],
        "series": sum(node["series"] for node in roots),
        "episodes": sum(node["episodes"] for node in roots),
    }
