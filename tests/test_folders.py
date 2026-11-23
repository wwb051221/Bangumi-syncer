from __future__ import annotations

from bangumi_syncer.folders import build_folder_tree, normalise_dir
from bangumi_syncer.models import SeriesRef


def series(path: str, episodes: int = 12, key: str = "k") -> SeriesRef:
    return SeriesRef(key=key, title=path, episode_count=episodes, library_path=path)


def test_normalise_dir():
    assert normalise_dir("/vol1/1000/影片/动漫/Show/S01/E01.mkv") == "/vol1/1000/影片/动漫/Show/S01"
    assert normalise_dir("vol1/a.mkv") == "/vol1"
    assert normalise_dir("") == ""
    assert normalise_dir("/") == ""


def test_builds_collapsed_tree():
    tree = build_folder_tree([
        series("/vol1/1000/影片/动漫/ShowA/S01/E01.mkv", 12, "a"),
        series("/vol1/1000/影片/动漫/ShowB/S01/E01.mkv", 24, "b"),
        series("/vol1/1000/影片/动漫/ShowA/S02/E01.mkv", 10, "a2"),
    ])
    assert tree["series"] == 3
    assert tree["episodes"] == 46
    root = tree["roots"][0]
    assert root["path"] == "/vol1/1000/影片/动漫"
    assert root["name"] == "动漫"
    assert root["series"] == 3
    child_paths = {child["path"] for child in root["children"]}
    # ShowA has two seasons so it is not collapsed, ShowB is
    assert child_paths == {
        "/vol1/1000/影片/动漫/ShowA",
        "/vol1/1000/影片/动漫/ShowB/S01",
    }
    show_a = next(child for child in root["children"] if child["name"] == "ShowA")
    assert show_a["series"] == 2
    assert show_a["episodes"] == 22


def test_multiple_roots_and_no_paths():
    tree = build_folder_tree([
        series("/vol1/影片/动漫/A/E01.mkv", 3, "a"),
        series("/vol2/动漫/B/E01.mkv", 4, "b"),
    ])
    assert len(tree["roots"]) == 2
    paths = {node["path"] for node in tree["roots"]}
    # single-child chains are collapsed down to the episode directory
    assert paths == {"/vol1/影片/动漫/A", "/vol2/动漫/B"}
    assert sum(node["series"] for node in tree["roots"]) == 2
    assert build_folder_tree([]) == {"roots": [], "series": 0, "episodes": 0}


def test_series_without_path_is_ignored():
    tree = build_folder_tree([SeriesRef(key="x", title="x", episode_count=5, library_path=None)])
    assert tree["roots"] == []
    assert tree["series"] == 0
