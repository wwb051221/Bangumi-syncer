"""Tests for the CLI entry points that do not touch the network."""

from __future__ import annotations

import json
import logging
import pathlib

import pytest
from conftest import ANIME_DIR

from bangumi_syncer import cli
from bangumi_syncer.state import StateStore


def write_config(tmp_path, fnos_db, *, users=("alice",), dry_run="true", extra="") -> str:
    lines = [
        "[app]",
        "dry_run = %s" % dry_run,
        'state_db = "%s"' % (tmp_path / "state.db").as_posix(),
        'timezone = "UTC"',
        "",
        "[source]",
        'mode = "snapshot"',
        'snapshot_path = "%s"' % pathlib.Path(fnos_db).as_posix(),
        'include_paths = ["%s"]' % ANIME_DIR,
        "",
        "[sync]",
        "verify_remote = false",
        "",
        "[target]",
        'user_agent = "bangumi-syncer-test/0"',
        "min_interval_seconds = 0",
        "",
        "[[target.accounts]]",
        'name = "main"',
        'client_id = ""',
        'client_secret = ""',
        "",
    ]
    for user in users:
        lines += ["[[users]]", 'fnos_user = "%s"' % user, 'account = "main"', ""]
    lines += ["[server]", "enabled = false", ""]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + extra, encoding="utf-8")
    return str(path)


def test_dump_json(tmp_path, fnos_db, capsys):
    config = write_config(tmp_path, fnos_db)
    assert cli.main(["--config", config, "dump", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"]["stats"]["episodes"] == 76  # 24 + 52 anime episodes
    assert {s["key"] for s in payload["series"]} == {"fnos:tv1:s1", "fnos:tv2:s1"}
    assert payload["series"][0]["title"]


def test_status_text(tmp_path, fnos_db, capsys):
    config = write_config(tmp_path, fnos_db)
    assert cli.main(["--config", config, "status"]) == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert "mappings=0" in out


def test_map_add_list_remove(tmp_path, fnos_db, capsys):
    config = write_config(tmp_path, fnos_db)
    assert cli.main([
        "--config", config, "map", "add",
        "--account", "main", "--series", "fnos:tv1:s1",
        "--subject", "420628", "--name", "药屋少女的呢喃",
    ]) == 0
    capsys.readouterr()

    assert cli.main(["--config", config, "map", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["subject_id"] == 420628
    assert rows[0]["confirmed"] is True

    assert cli.main([
        "--config", config, "map", "remove", "--account", "main", "--series", "fnos:tv1:s1",
    ]) == 0
    capsys.readouterr()
    assert cli.main(["--config", config, "map", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_review_lists_candidates(tmp_path, fnos_db, capsys):
    config = write_config(tmp_path, fnos_db)
    store = StateStore(str(tmp_path / "state.db"))
    store.save_candidates("main", "fnos:x:s1", {
        "title": "未匹配番组",
        "season": 1,
        "year": 2016,
        "episode_count": 21,
        "reason": "best candidate 0.394 < min_score 0.860",
        "candidates": [{"subject_id": 148295, "name": "茶啊二中", "name_cn": "茶啊二中",
                        "year": 2014, "eps": 10, "score": 0.394}],
    })
    store.close()

    assert cli.main(["--config", config, "review", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["title"] == "未匹配番组"
    assert payload[0]["candidates"][0]["subject_id"] == 148295

    assert cli.main(["--config", config, "review"]) == 0
    text = capsys.readouterr().out
    assert "未匹配番组" in text
    assert "bangumi-syncer map add" in text


def test_run_once_offline_when_nothing_finished(tmp_path, fnos_db, capsys, caplog):
    # bob only started episode 3 (6.9%), so no search or push should happen
    caplog.set_level(logging.INFO)
    config = write_config(tmp_path, fnos_db, users=("bob",))
    assert cli.main(["--config", config, "run", "--once"]) == 0
    out = capsys.readouterr().out
    assert "pushed=0" in out
    assert any("nothing finished" in record.getMessage() for record in caplog.records)


def test_run_once_needs_a_token_when_live(tmp_path, fnos_db, capsys):
    config = write_config(tmp_path, fnos_db, dry_run="false")
    assert cli.main(["--config", config, "run", "--once"]) == 3
    captured = capsys.readouterr()
    assert "token" in (captured.out + captured.err).lower()


def test_version_and_config_error(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0

    assert cli.main(["--config", str(tmp_path / "missing.toml"), "status"]) == 2
