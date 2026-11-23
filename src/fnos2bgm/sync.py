"""The sync engine.

One run is: snapshot the media server -> for every configured user figure out
which episodes are finished -> map the local season to a Bangumi subject ->
push the missing episode marks -> keep the collection status coherent.

Guarantees
----------
* **idempotent**: a pushed episode is never pushed twice (state table), and
  ``sync.verify_remote`` additionally checks Bangumi's own episode marks.
* **safe by default**: ``app.dry_run`` defaults to true, so a fresh install
  only logs what it *would* do.
* **per user**: each fnOS user is mapped to a Bangumi account independently.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

from .config import Config, UserConfig
from .log import get_logger
from .matcher import Matcher
from .models import PlayState, SeriesRef, SourceSnapshot, is_watched
from .sources import SourceAdapter
from .sources.fnos import path_allowed
from .state import StateStore
from .trackers import COLLECTED, DOING, EP_COLLECTED, AuthError, Tracker, TrackerError
from .trackers.base import TrackEpisode

log = get_logger("sync")


@dataclass(slots=True)
class UserResult:
    user: str
    account: str
    series_seen: int = 0
    matched: int = 0
    unmatched: int = 0
    pushed: int = 0
    skipped: int = 0
    failed: int = 0
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "user": self.user,
            "account": self.account,
            "series_seen": self.series_seen,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "pushed": self.pushed,
            "skipped": self.skipped,
            "failed": self.failed,
            "error": self.error,
        }


@dataclass(slots=True)
class RunResult:
    started_at: int
    finished_at: int = 0
    ok: bool = True
    dry_run: bool = True
    users: list[UserResult] = field(default_factory=list)
    error: str | None = None
    duration_seconds: float = 0.0

    @property
    def pushed(self) -> int:
        return sum(u.pushed for u in self.users)

    @property
    def skipped(self) -> int:
        return sum(u.skipped for u in self.users)

    @property
    def failed(self) -> int:
        return sum(u.failed for u in self.users)

    @property
    def unmatched(self) -> int:
        return sum(u.unmatched for u in self.users)

    @property
    def matched(self) -> int:
        return sum(u.matched for u in self.users)

    def summary(self) -> str:
        return (
            "users=%d pushed=%d skipped=%d failed=%d matched=%d unmatched=%d%s"
            % (
                len(self.users),
                self.pushed,
                self.skipped,
                self.failed,
                self.matched,
                self.unmatched,
                " (dry-run)" if self.dry_run else "",
            )
        )

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 2),
            "ok": self.ok,
            "dry_run": self.dry_run,
            "error": self.error,
            "pushed": self.pushed,
            "skipped": self.skipped,
            "failed": self.failed,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "users": [u.as_dict() for u in self.users],
        }


class _AccountSession:
    """Per-account tracker plus run-scoped caches (episodes, marks, collection)."""

    def __init__(self, tracker: Tracker, verify_remote: bool) -> None:
        self.tracker = tracker
        self.verify_remote = verify_remote
        self._episodes: dict[int, list[TrackEpisode]] = {}
        self._marks: dict[int, dict[int, int]] = {}
        self._collection: dict[int, int | None] = {}
        self._search: dict[str, list] = {}

    def episodes(self, subject_id: int) -> list[TrackEpisode]:
        if subject_id not in self._episodes:
            episodes = sorted(self.tracker.episodes(subject_id), key=lambda e: e.number)
            self._episodes[subject_id] = episodes
        return self._episodes[subject_id]

    def marks(self, subject_id: int) -> dict[int, int]:
        if not self.verify_remote:
            return {}
        if subject_id not in self._marks:
            try:
                self._marks[subject_id] = self.tracker.episode_marks(subject_id)
            except TrackerError as exc:
                log.warning("could not read remote episode marks for subject %d: %s", subject_id, exc)
                self._marks[subject_id] = {}
        return self._marks[subject_id]

    def collection(self, subject_id: int) -> int | None:
        if subject_id not in self._collection:
            self._collection[subject_id] = self.tracker.collection(subject_id)
        return self._collection[subject_id]

    def set_collection(self, subject_id: int, status: int) -> bool:
        if self.collection(subject_id) == status:
            return False
        self.tracker.set_collection(subject_id, status)
        self._collection[subject_id] = status
        return True

    def search(self, term: str, limit: int) -> list:
        key = "%s|%d" % (term, limit)
        if key not in self._search:
            self._search[key] = self.tracker.search(term, limit)
        return self._search[key]


class SyncEngine:
    def __init__(
        self,
        cfg: Config,
        store: StateStore,
        source: SourceAdapter,
        tracker_factory: Callable[[str], Tracker] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.source = source
        self.matcher = Matcher(cfg.match)
        self._factory = tracker_factory or self._default_tracker
        self._sessions: dict[str, _AccountSession] = {}
        self._sessions_ready: set[str] = set()

    # ------------------------------------------------------------- plumbing
    def _default_tracker(self, account_name: str) -> Tracker:
        from . import trackers

        account_cfg = self.cfg.account(account_name)
        return trackers.create(account_cfg, self.cfg.target, self.store)

    def session(self, account_name: str) -> _AccountSession:
        if account_name not in self._sessions:
            self._sessions[account_name] = _AccountSession(
                self._factory(account_name), self.cfg.sync.verify_remote
            )
        session = self._sessions[account_name]
        if account_name not in self._sessions_ready:
            # A dry run only uses public endpoints (search, episode lists), so it
            # works before `fnos2bgm oauth` has ever been run.
            if not self.cfg.app.dry_run:
                session.tracker.ensure_ready()
            else:
                log.debug("dry-run: skipping credential check for account %r", account_name)
            self._sessions_ready.add(account_name)
        return session

    # ------------------------------------------------------------------ run
    def run(self) -> RunResult:
        started = time.monotonic()
        result = RunResult(started_at=int(time.time()), dry_run=self.cfg.app.dry_run)
        run_id = self.store.start_run()
        try:
            self.source.prepare()
            snapshot = self.source.snapshot()
            by_user = snapshot.by_user()
            log.info(
                "source snapshot: %d users, %d plays, %d configured user(s)",
                len(snapshot.users),
                len(snapshot.plays),
                len(self.cfg.enabled_users()),
            )
            for user_cfg in self.cfg.enabled_users():
                self._run_user(user_cfg, snapshot, by_user, result)
        except AuthError as exc:
            result.ok = False
            result.error = str(exc)
            log.error("authentication problem: %s", exc)
        except Exception as exc:  # noqa: BLE001 - one bad run must not kill the container
            result.ok = False
            result.error = "%s: %s" % (type(exc).__name__, exc)
            log.exception("sync run failed")
        result.finished_at = int(time.time())
        result.duration_seconds = time.monotonic() - started
        self.store.finish_run(
            run_id,
            result.ok,
            users=len(result.users),
            pushed=result.pushed,
            failed=result.failed,
            matched=result.matched,
            unmatched=result.unmatched,
            message=result.error,
        )
        log.info("run finished in %.1fs: %s", result.duration_seconds, result.summary())
        return result

    # ------------------------------------------------------------- one user
    def _run_user(
        self,
        user_cfg: UserConfig,
        snapshot: SourceSnapshot,
        by_user: dict[str, list[PlayState]],
        run: RunResult,
    ) -> None:
        user = _find_user(snapshot, user_cfg.fnos_user)
        if user is None:
            log.warning(
                "user %r has no play history in fnOS (known users: %s)",
                user_cfg.fnos_user,
                ", ".join(u.name for u in snapshot.users) or "none",
            )
            run.users.append(UserResult(user_cfg.fnos_user, user_cfg.account, error="user not found in media server"))
            return

        result = UserResult(user=user.name, account=user_cfg.account)
        run.users.append(result)

        ratio = user_cfg.watch_ratio if user_cfg.watch_ratio is not None else self.cfg.sync.watch_ratio
        plays = [p for p in by_user.get(user.key, [])]
        if user_cfg.include_paths:
            plays = [
                p
                for p in plays
                if path_allowed(p.episode.series.library_path, user_cfg.include_paths, self.cfg.source.exclude_paths)
            ]
        watched = [p for p in plays if is_watched(p, ratio, self.cfg.sync.min_position_seconds)]
        if not watched:
            log.info("user %s: nothing finished yet (%d play records)", user.name, len(plays))
            return

        try:
            session = self.session(user_cfg.account)
        except AuthError as exc:
            result.error = str(exc)
            raise

        groups: dict[str, list[PlayState]] = {}
        for play in watched:
            groups.setdefault(play.episode.series.key, []).append(play)
        result.series_seen = len(groups)
        log.info("user %s -> account %s: %d finished episode(s) in %d show(s)",
                 user.name, user_cfg.account, len(watched), len(groups))

        for series_key, group in groups.items():
            try:
                self._sync_series(user_cfg, session, group, result)
            except AuthError:
                raise
            except TrackerError as exc:
                result.failed += 1
                log.warning("series %s: %s", series_key, exc)
            except Exception:  # noqa: BLE001
                result.failed += 1
                log.exception("series %s: unexpected failure", series_key)

    # ------------------------------------------------------------ one season
    def _sync_series(
        self,
        user_cfg: UserConfig,
        session: _AccountSession,
        group: list[PlayState],
        result: UserResult,
    ) -> None:
        account = user_cfg.account
        series = group[0].episode.series

        # newest play per episode number
        latest: dict[int, PlayState] = {}
        for play in group:
            previous = latest.get(play.episode.number)
            if previous is None or play.last_played_at >= previous.last_played_at:
                latest[play.episode.number] = play
        numbers = sorted(latest)

        mappings = self._resolve_mappings(session, account, series, numbers, result)
        if not mappings:
            return

        by_subject: dict[int, tuple[object, list[int]]] = {}
        for number, mapping in mappings.items():
            by_subject.setdefault(int(mapping["subject_id"]), (mapping, []))[1].append(number)

        for subject_id, (mapping, subject_numbers) in by_subject.items():
            episodes = session.episodes(subject_id)
            if not episodes:
                self._record_failure(account, series, subject_numbers, subject_id,
                                     "subject %d has no main episodes on Bangumi" % subject_id)
                result.failed += len(subject_numbers)
                continue
            marks = session.marks(subject_id)

            ep_from = int(mapping["ep_from"] or 1)
            watched_indices: set[int] = set()
            for number in sorted(subject_numbers):
                index = number - ep_from
                if index < 0 or index >= len(episodes):
                    self._record_failure(
                        account,
                        series,
                        [number],
                        subject_id,
                        "subject %d has %d episode(s); local episode %d maps to #%d - a split mapping "
                        "may be needed (fnos2bgm map add --series %s --subject <id> --from N --to M)"
                        % (subject_id, len(episodes), number, index + 1, series.key),
                        state="unmapped",
                    )
                    continue
                watched_indices.add(index)
                target = episodes[index]
                self._push_episode(user_cfg, session, series, number, mapping, target, marks, result)

            self._update_collection(user_cfg, session, series, mapping, episodes, watched_indices, result)

    # ------------------------------------------------------------- mapping
    def _resolve_mappings(
        self,
        session: _AccountSession,
        account: str,
        series: SeriesRef,
        numbers: list[int],
        result: UserResult,
    ) -> dict[int, object]:
        """Return ``episode number -> mapping row`` for the watched episodes."""
        existing_groups = self.store.mapping_groups(account, series.key)
        mappings: dict[int, object] = {}
        missing: list[int] = []
        for number in numbers:
            mapping = self.store.mapping_for(account, series.key, number)
            if mapping is None:
                missing.append(number)
            else:
                mappings[number] = mapping

        if not missing:
            result.matched += 1
            return mappings

        if not existing_groups:
            decision, hits = self._auto_match(session, series)
            if decision.matched and decision.hit is not None:
                self.store.save_mapping(
                    account,
                    series.key,
                    subject_id=decision.hit.subject_id,
                    subject_name=decision.hit.display_name,
                    strategy="auto",
                    confidence=round(decision.score, 4),
                    ep_from=1,
                    ep_to=9999,
                    confirmed=self.cfg.match.auto_confirm,
                )
                self.store.delete_candidates(account, series.key)
                result.matched += 1
                return {number: self.store.mapping_for(account, series.key, number) for number in numbers}
            result.unmatched += 1
            log.warning(
                "no confident Bangumi match for %r (season %d, %d eps): %s -- review with "
                "`fnos2bgm review` then `fnos2bgm map add`",
                series.title,
                series.season,
                series.episode_count,
                decision.reason,
            )
            return mappings

        # Partially mapped (e.g. a merged season): keep the mapped part, flag the rest.
        if missing:
            result.unmatched += 1
            log.warning(
                "%r S%d: %d episode(s) not covered by any mapping (e.g. %d) - add a range mapping with "
                "`fnos2bgm map add --series %s --subject <id> --from N --to M`",
                series.title,
                series.season,
                len(missing),
                missing[0],
                series.key,
            )
        if mappings:
            result.matched += 1
        return mappings

    def _auto_match(self, session: _AccountSession, series: SeriesRef):
        hits = []
        seen: set[int] = set()
        limit = max(self.cfg.match.search_limit, 5)
        for term in series.search_terms():
            try:
                for hit in session.search(term, limit):
                    if hit.subject_id not in seen:
                        seen.add(hit.subject_id)
                        hits.append(hit)
            except TrackerError as exc:
                log.warning("search %r failed: %s", term, exc)
        decision = self.matcher.decide(series, hits)
        account = session.tracker.account
        if not decision.matched:
            self.store.save_candidates(
                account,
                series.key,
                {
                    "title": series.title,
                    "original_title": series.original_title,
                    "season": series.season,
                    "year": series.year,
                    "episode_count": series.episode_count,
                    "reason": decision.reason,
                    "candidates": decision.candidates,
                },
            )
        if decision.matched and decision.hit is not None:
            log.info(
                "matched %r S%d -> %d %r (score %.2f, %d candidate(s))",
                series.title,
                series.season,
                decision.hit.subject_id,
                decision.hit.display_name,
                decision.score,
                len(hits),
            )
        return decision, hits

    # ---------------------------------------------------------------- push
    def _push_episode(
        self,
        user_cfg: UserConfig,
        session: _AccountSession,
        series: SeriesRef,
        number: int,
        mapping,
        target: TrackEpisode,
        marks: dict[int, int],
        result: UserResult,
    ) -> None:
        account = user_cfg.account
        subject_id = int(mapping["subject_id"])
        existing = self.store.push(account, series.key, number)
        if existing is not None and existing["state"] in ("sent", "unmapped"):
            result.skipped += 1
            return
        if marks.get(target.episode_id) == EP_COLLECTED:
            self.store.record_push(
                account, series.key, number, "skipped",
                subject_id=subject_id, episode_id=target.episode_id,
                position_seconds=0, watched=True,
            )
            result.skipped += 1
            return

        label = "%r S%dE%d -> subject %d ep %s (%s)" % (
            series.title,
            series.season,
            number,
            subject_id,
            target.number,
            target.display_name or "?",
        )
        if self.cfg.app.dry_run:
            log.info("[dry-run] would mark watched: %s", label)
            result.pushed += 1
            return
        try:
            session.tracker.mark_episode(subject_id, target.episode_id)
        except AuthError:
            raise
        except TrackerError as exc:
            self.store.record_push(
                account, series.key, number, "failed",
                subject_id=subject_id, episode_id=target.episode_id, error=str(exc),
            )
            result.failed += 1
            log.warning("failed to mark %s: %s", label, exc)
            return
        self.store.record_push(
            account, series.key, number, "sent",
            subject_id=subject_id, episode_id=target.episode_id, watched=True,
        )
        result.pushed += 1
        log.info("marked watched: %s", label)

    def _record_failure(
        self,
        account: str,
        series: SeriesRef,
        numbers: Iterable[int],
        subject_id: int,
        message: str,
        state: str = "failed",
    ) -> None:
        for number in numbers:
            self.store.record_push(account, series.key, number, state, subject_id=subject_id, error=message)
        log.warning("%s S%d: %s", series.title, series.season, message)

    # ------------------------------------------------------------ collection
    def _update_collection(
        self,
        user_cfg: UserConfig,
        session: _AccountSession,
        series: SeriesRef,
        mapping,
        episodes: list[TrackEpisode],
        watched_indices: set[int],
        result: UserResult,
    ) -> None:
        if not (self.cfg.sync.mark_in_progress or self.cfg.sync.mark_completed):
            return
        subject_id = int(mapping["subject_id"])
        aired = [i for i, episode in enumerate(episodes) if _is_aired(episode)]
        complete = bool(aired) and all(i in watched_indices for i in aired)
        if complete and self.cfg.sync.mark_completed:
            desired = COLLECTED
        elif not complete and self.cfg.sync.mark_in_progress:
            desired = DOING
        else:
            return
        label = "%r -> subject %d as %s" % (
            series.title,
            subject_id,
            "collected" if desired == COLLECTED else "watching",
        )
        if self.cfg.app.dry_run:
            log.info("[dry-run] would set collection: %s", label)
            return
        try:
            if session.set_collection(subject_id, desired):
                log.info("collection updated: %s", label)
        except AuthError:
            raise
        except TrackerError as exc:
            result.failed += 1
            log.warning("could not update collection for subject %d: %s", subject_id, exc)


def _find_user(snapshot: SourceSnapshot, wanted: str):
    for user in snapshot.users:
        if user.name == wanted or user.key == wanted:
            return user
    return None


def _is_aired(episode: TrackEpisode) -> bool:
    if not episode.airdate:
        return True
    try:
        return date.fromisoformat(episode.airdate) <= date.today()
    except ValueError:
        return True
