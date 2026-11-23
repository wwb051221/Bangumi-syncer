"""Runtime-editable settings.

The TOML file stays the source of truth for secrets, mounts and listener
address, but everything an operator may want to change at 2 a.m. lives here:
sync switches, thresholds, library filters and **which fnOS users to sync**.

Values are applied to the live ``Config`` object in memory and persisted in the
state database, so a restart keeps them. The sync engine reads the config on
every run, which means a change takes effect on the next cycle without a
container restart.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any

from . import sources
from .config import Config, UserConfig
from .folders import build_folder_tree
from .log import get_logger
from .models import is_watched
from .state import StateStore

log = get_logger("settings")

SETTINGS_KEY = "runtime_settings"


class SettingsError(Exception):
    """Invalid settings payload (surfaced as HTTP 400)."""


# --------------------------------------------------------------------- helpers
def _as_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if isinstance(value, bool):
        return value
    raise SettingsError("%s must be a boolean" % key)


def _as_int(payload: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError("%s must be a number" % key)
    number = int(value)
    if number < low or number > high:
        raise SettingsError("%s must be between %d and %d" % (key, low, high))
    return number


def _as_float(payload: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SettingsError("%s must be a number" % key)
    number = float(value)
    if number < low or number > high:
        raise SettingsError("%s must be between %.2f and %.2f" % (key, low, high))
    return round(number, 4)


def _as_paths(payload: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = payload.get(key, default)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SettingsError("%s must be a list of strings" % key)
    if len(value) > 32:
        raise SettingsError("%s has too many entries (max 32)" % key)
    paths: list[str] = []
    for item in value:
        text = item.strip().replace("\\", "/")
        if not text:
            continue
        if not text.startswith("/"):
            text = "/" + text
        paths.append(text.rstrip("/"))
    return paths


def _normalise_accounts(cfg: Config) -> list[str]:
    return [account.name for account in cfg.target.accounts]


def validate(payload: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """Validate a (possibly partial, already merged) settings payload."""
    match_payload = payload.get("match") or {}
    if not isinstance(match_payload, dict):
        raise SettingsError("match must be an object")

    users_payload = payload.get("users")
    if users_payload is None:
        raise SettingsError("users is required")
    if not isinstance(users_payload, list) or not users_payload:
        raise SettingsError("users must list at least one account")
    accounts = _normalise_accounts(cfg)
    users: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(users_payload):
        if not isinstance(raw, dict):
            raise SettingsError("users[%d] must be an object" % index)
        fnos_user = str(raw.get("fnos_user") or "").strip()
        if not fnos_user:
            raise SettingsError("users[%d].fnos_user is required" % index)
        if fnos_user in seen:
            continue
        account = str(raw.get("account") or "").strip() or accounts[0]
        if account not in accounts:
            raise SettingsError("users[%d] references unknown account %r" % (index, account))
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SettingsError("users[%d].enabled must be a boolean" % index)
        ratio = raw.get("watch_ratio")
        if ratio is not None:
            if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0.05 <= float(ratio) <= 1.0:
                raise SettingsError("users[%d].watch_ratio must be between 0.05 and 1.0 or null" % index)
            ratio = round(float(ratio), 4)
        users.append({"fnos_user": fnos_user, "account": account, "enabled": enabled, "watch_ratio": ratio})
        seen.add(fnos_user)

    return {
        "dry_run": _as_bool(payload, "dry_run", cfg.app.dry_run),
        "interval_seconds": _as_int(payload, "interval_seconds", cfg.sync.interval_seconds, 30, 86400),
        "watch_ratio": _as_float(payload, "watch_ratio", cfg.sync.watch_ratio, 0.05, 1.0),
        "min_position_seconds": _as_int(payload, "min_position_seconds", cfg.sync.min_position_seconds, 0, 3600),
        "verify_remote": _as_bool(payload, "verify_remote", cfg.sync.verify_remote),
        "mark_in_progress": _as_bool(payload, "mark_in_progress", cfg.sync.mark_in_progress),
        "mark_completed": _as_bool(payload, "mark_completed", cfg.sync.mark_completed),
        "include_paths": _as_paths(payload, "include_paths", list(cfg.source.include_paths)),
        "exclude_paths": _as_paths(payload, "exclude_paths", list(cfg.source.exclude_paths)),
        "skip_season_zero": _as_bool(payload, "skip_season_zero", cfg.source.skip_season_zero),
        "min_episodes": _as_int(payload, "min_episodes", cfg.source.min_episodes, 1, 1000),
        "match": {
            "min_score": _as_float(match_payload, "min_score", cfg.match.min_score, 0.0, 1.0),
            "min_margin": _as_float(match_payload, "min_margin", cfg.match.min_margin, 0.0, 1.0),
            "search_limit": _as_int(match_payload, "search_limit", cfg.match.search_limit, 1, 20),
        },
        "users": users,
    }


def current_values(cfg: Config) -> dict[str, Any]:
    return {
        "dry_run": bool(cfg.app.dry_run),
        "interval_seconds": int(cfg.sync.interval_seconds),
        "watch_ratio": float(cfg.sync.watch_ratio),
        "min_position_seconds": int(cfg.sync.min_position_seconds),
        "verify_remote": bool(cfg.sync.verify_remote),
        "mark_in_progress": bool(cfg.sync.mark_in_progress),
        "mark_completed": bool(cfg.sync.mark_completed),
        "include_paths": list(cfg.source.include_paths),
        "exclude_paths": list(cfg.source.exclude_paths),
        "skip_season_zero": bool(cfg.source.skip_season_zero),
        "min_episodes": int(cfg.source.min_episodes),
        "match": {
            "min_score": float(cfg.match.min_score),
            "min_margin": float(cfg.match.min_margin),
            "search_limit": int(cfg.match.search_limit),
        },
        "users": [
            {"fnos_user": user.fnos_user, "account": user.account,
             "enabled": bool(user.enabled), "watch_ratio": user.watch_ratio}
            for user in cfg.users
        ],
    }


def apply_values(cfg: Config, values: dict[str, Any]) -> None:
    cfg.app.dry_run = bool(values["dry_run"])
    cfg.sync.interval_seconds = int(values["interval_seconds"])
    cfg.sync.watch_ratio = float(values["watch_ratio"])
    cfg.sync.min_position_seconds = int(values["min_position_seconds"])
    cfg.sync.verify_remote = bool(values["verify_remote"])
    cfg.sync.mark_in_progress = bool(values["mark_in_progress"])
    cfg.sync.mark_completed = bool(values["mark_completed"])
    cfg.source.include_paths = list(values["include_paths"])
    cfg.source.exclude_paths = list(values["exclude_paths"])
    cfg.source.skip_season_zero = bool(values["skip_season_zero"])
    cfg.source.min_episodes = int(values["min_episodes"])
    cfg.match.min_score = float(values["match"]["min_score"])
    cfg.match.min_margin = float(values["match"]["min_margin"])
    cfg.match.search_limit = int(values["match"]["search_limit"])
    cfg.users = [
        UserConfig(
            fnos_user=user["fnos_user"],
            account=user["account"],
            enabled=bool(user["enabled"]),
            watch_ratio=user["watch_ratio"],
        )
        for user in values["users"]
    ]


class SettingsManager:
    """Applies, persists and exposes the editable settings."""

    def __init__(self, cfg: Config, store: StateStore) -> None:
        self.cfg = cfg
        self.store = store
        self._lock = threading.RLock()
        self._baseline = current_values(cfg)
        self._source_cache: dict[str, Any] | None = None
        self._source_at = 0.0
        stored = store.get_json(SETTINGS_KEY)
        if isinstance(stored, dict):
            try:
                merged = _deep_merge(self._baseline, stored)
                apply_values(cfg, validate(merged, cfg))
                log.info("applied persisted runtime settings")
            except SettingsError as exc:
                log.warning("ignoring persisted runtime settings: %s", exc)

    # ------------------------------------------------------------------ read
    def values(self) -> dict[str, Any]:
        with self._lock:
            return current_values(self.cfg)

    @property
    def interval_seconds(self) -> int:
        return int(self.cfg.sync.interval_seconds)

    @property
    def accounts(self) -> list[str]:
        return _normalise_accounts(self.cfg)

    def source_users(self, force: bool = False) -> dict[str, Any]:
        """List the users that actually exist in the media server."""
        with self._lock:
            if self._source_cache is not None and not force and time.time() - self._source_at < 60:
                return self._source_cache
        source = sources.create(self.cfg.source)
        try:
            source.prepare()
            snapshot = source.snapshot()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            raise SettingsError("读取飞牛影视数据失败：%s" % exc) from exc
        by_user = snapshot.by_user()
        ratio = float(self.cfg.sync.watch_ratio)
        min_position = int(self.cfg.sync.min_position_seconds)
        users = []
        for user in snapshot.users:
            plays = by_user.get(user.key, [])
            users.append({
                "key": user.key,
                "name": user.name,
                "plays": len(plays),
                "finished": sum(1 for play in plays if is_watched(play, ratio, min_position)),
            })
        users.sort(key=lambda item: (-item["plays"], item["name"]))

        # The picker must also show folders that are currently filtered out,
        # otherwise an excluded library could never be re-enabled.
        tree_series = snapshot.series
        if self.cfg.source.include_paths or self.cfg.source.exclude_paths:
            try:
                unfiltered = replace(self.cfg.source, include_paths=[], exclude_paths=[])
                extra = sources.create(unfiltered)
                extra.prepare()
                tree_series = extra.snapshot().series
            except Exception as exc:  # noqa: BLE001 - the tree is best effort
                log.warning("could not read the full library for the folder picker: %s", exc)

        payload = {
            "users": users,
            "seasons": len(snapshot.series),
            "episodes": sum(series.episode_count for series in snapshot.series),
            "folders": build_folder_tree(tree_series),
            "include_paths": list(self.cfg.source.include_paths),
            "at": int(time.time()),
        }
        with self._lock:
            self._source_cache = payload
            self._source_at = time.time()
        return payload

    # ----------------------------------------------------------------- write
    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise SettingsError("expected a JSON object")
        with self._lock:
            merged = _deep_merge(self.values(), payload)
            values = validate(merged, self.cfg)
            apply_values(self.cfg, values)
            self.store.set_json(SETTINGS_KEY, values)
            self._source_cache = None
        log.info("runtime settings updated (dry_run=%s, users=%d, interval=%ss)",
                 values["dry_run"], len(values["users"]), values["interval_seconds"])
        return self.values()

    def reset(self) -> dict[str, Any]:
        with self._lock:
            apply_values(self.cfg, validate(_deep_merge(self._baseline, {}), self.cfg))
            self.store.delete_meta(SETTINGS_KEY)
            self._source_cache = None
        log.info("runtime settings reset to config.toml values")
        return self.values()


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if key == "match" and isinstance(value, dict):
            merged["match"] = {**base.get("match", {}), **value}
        else:
            merged[key] = value
    return merged
