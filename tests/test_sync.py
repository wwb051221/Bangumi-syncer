from __future__ import annotations

from conftest import FakeTracker, make_config, make_episodes

from bangumi_syncer import sources
from bangumi_syncer.state import StateStore
from bangumi_syncer.sync import SyncEngine
from bangumi_syncer.trackers import COLLECTED, DOING, AuthError, SubjectHit, TrackerError

PHARMACY_CN = "\u836f\u5c4b\u5c11\u5973\u7684\u5462\u5583"
PHARMACY_JP = "\u85ac\u5c4b\u306e\u3072\u3068\u308a\u3054\u3068"
SUBJECT = 420628


def build_engine(cfg, fake: FakeTracker):
    store = StateStore(cfg.app.state_db)
    source = sources.create(cfg.source)
    engine = SyncEngine(cfg, store, source, tracker_factory=lambda _name: fake)
    return engine, store


def prepare_pharmacy(fake: FakeTracker) -> None:
    fake.subjects[SUBJECT] = make_episodes(24)
    fake.hits[PHARMACY_CN] = [SubjectHit(SUBJECT, PHARMACY_JP, PHARMACY_CN, 2023, 24)]
    fake.hits[PHARMACY_JP] = [SubjectHit(SUBJECT, PHARMACY_JP, PHARMACY_CN, 2023, 24)]


def test_dry_run_never_writes(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=True)
    fake = FakeTracker()
    prepare_pharmacy(fake)
    engine, store = build_engine(cfg, fake)

    result = engine.run()

    assert result.ok
    assert result.pushed == 2  # ep1 finished + ep2 at 90.3% of 24min
    assert fake.marked == []
    assert fake.collections == {}
    assert store.pushes() == []
    rows = store.mappings("main")
    assert len(rows) == 1 and rows[0]["subject_id"] == SUBJECT
    store.close()


def test_live_push_is_idempotent(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=False)
    fake = FakeTracker()
    prepare_pharmacy(fake)
    engine, store = build_engine(cfg, fake)

    result = engine.run()
    assert fake.marked == [(SUBJECT, 1000, 2), (SUBJECT, 1001, 2)]
    assert result.pushed == 2
    assert fake.collections[SUBJECT] == DOING
    sent = [row for row in store.pushes() if row["state"] == "sent"]
    assert {row["episode"] for row in sent} == {1, 2}

    # a second run must not push anything again
    second = engine.run()
    assert second.pushed == 0
    assert len(fake.marked) == 2
    store.close()


def test_remote_marks_are_respected(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=False, verify_remote=True)
    fake = FakeTracker()
    prepare_pharmacy(fake)
    fake.remote_marks[SUBJECT] = {1000: 2}  # episode 1 already marked on Bangumi
    engine, store = build_engine(cfg, fake)

    engine.run()

    assert fake.marked == [(SUBJECT, 1001, 2)]
    states = {row["episode"]: row["state"] for row in store.pushes()}
    assert states[1] == "skipped"
    assert states[2] == "sent"
    store.close()


def test_split_mapping_uses_episode_offset(tmp_path, fnos_db):
    """A merged season: local 27..52 maps to the second subject's episodes 1..26."""
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=False)
    fake = FakeTracker()
    fake.subjects[2000] = make_episodes(26, base_id=5000)
    engine, store = build_engine(cfg, fake)
    store.save_mapping("main", "fnos:tv2:s1", subject_id=2000, subject_name="S2",
                       strategy="manual", ep_from=27, ep_to=52, confirmed=True)

    result = engine.run()

    # alice finished local episode 30 -> index 3 of the subject -> episode id 5003
    assert (2000, 5003, 2) in fake.marked
    # the mapped series must not be searched at all (the pharmacy still is, it has no mapping)
    assert "\u8fdb\u51fb\u7684\u5de8\u4eba" not in fake.search_terms
    assert "\u9032\u6483\u306e\u5de8\u4eba" not in fake.search_terms
    assert result.pushed >= 1
    store.close()


def test_episode_beyond_subject_is_recorded_as_unmapped(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=False)
    fake = FakeTracker()
    fake.subjects[2000] = make_episodes(2)  # deliberately too short
    engine, store = build_engine(cfg, fake)
    store.save_mapping("main", "fnos:tv2:s1", subject_id=2000, subject_name="S2",
                       strategy="manual", ep_from=1, ep_to=9999, confirmed=True)

    result = engine.run()

    assert fake.marked == []
    assert result.failed == 0
    row = store.push("main", "fnos:tv2:s1", 30)
    assert row["state"] == "unmapped"
    assert "split mapping" in row["error"]
    store.close()


def test_completed_subject_is_collected(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=False)
    fake = FakeTracker()
    fake.subjects[500] = make_episodes(2, base_id=7000)
    engine, store = build_engine(cfg, fake)
    store.save_mapping("main", "fnos:tv1:s1", subject_id=500, subject_name="short",
                       strategy="manual", ep_from=1, ep_to=9999, confirmed=True)

    engine.run()

    assert fake.marked == [(500, 7000, 2), (500, 7001, 2)]
    assert fake.collections[500] == COLLECTED
    store.close()


def test_unmatched_show_goes_to_review(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=False)
    fake = FakeTracker()  # no search hits at all
    engine, store = build_engine(cfg, fake)

    result = engine.run()

    assert result.ok
    assert result.unmatched == 2  # alice finished episodes of two different shows
    assert store.mappings("main") == []
    review = store.candidates("main")
    assert len(review) == 2
    titles = {row["payload"]["title"] for row in review}
    assert titles == {PHARMACY_CN, "\u8fdb\u51fb\u7684\u5de8\u4eba"}
    assert all(row["payload"]["candidates"] == [] for row in review)
    store.close()


def test_push_failure_is_recorded_not_fatal(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=False)
    fake = FakeTracker()
    prepare_pharmacy(fake)
    fake.fail_marks = TrackerError("bangumi is angry")
    engine, store = build_engine(cfg, fake)

    result = engine.run()

    assert result.ok  # the run survives a single failing series
    assert result.failed == 2
    states = {row["state"] for row in store.pushes()}
    assert states == {"failed"}
    store.close()


def test_auth_error_is_reported(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=False)
    fake = FakeTracker()
    fake.auth_error = AuthError("no token for account 'main'")
    engine, store = build_engine(cfg, fake)

    result = engine.run()

    assert not result.ok
    assert "no token" in (result.error or "")
    assert fake.marked == []
    store.close()


def test_search_types_follow_config(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=False)
    cfg.match.accept_types = [2, 6]
    fake = FakeTracker()
    engine, store = build_engine(cfg, fake)

    engine.run()

    assert fake.search_types
    assert all(types == [2, 6] for types in fake.search_types)
    store.close()


def test_users_without_finished_episodes_are_reported(tmp_path, fnos_db):
    cfg = make_config(fnos_db, str(tmp_path / "state.db"), dry_run=False)
    fake = FakeTracker()
    prepare_pharmacy(fake)
    engine, store = build_engine(cfg, fake)

    result = engine.run()

    by_name = {u.user: u for u in result.users}
    assert by_name["alice"].pushed == 2
    assert by_name["bob"].pushed == 0  # only started ep3, nothing finished
    assert by_name["bob"].error is None
    store.close()
