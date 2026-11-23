"""Configuration loading.

The file format is TOML (stdlib ``tomllib``) so no third party parser is
needed. Strings support ``${VAR}`` / ``${VAR:-default}`` expansion, which lets
secrets live in the container environment instead of the config file.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import asdict, dataclass, field
from typing import Any

from .log import get_logger

log = get_logger("config")

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULT_CONFIG_PATHS = ("/config/config.toml", "config.toml")
BUILTIN_SOURCE_TYPES = ("fnos",)
BUILTIN_TARGET_TYPES = ("bangumi",)


class ConfigError(Exception):
    pass


# --------------------------------------------------------------------- models
@dataclass(slots=True)
class AppConfig:
    timezone: str = "Asia/Shanghai"
    dry_run: bool = True
    state_db: str = "/data/state/state.db"
    log_level: str = "INFO"
    log_format: str = "text"  # text | json


@dataclass(slots=True)
class FnosOptions:
    """Escape hatches for fnOS schema drift. Empty means "use the built-in SQL"."""

    episodes_query: str = ""
    plays_query: str = ""
    users_query: str = ""


@dataclass(slots=True)
class SourceConfig:
    type: str = "fnos"
    mode: str = "live"  # live: snapshot the server DB ourselves | snapshot: read a prepared copy
    db_path: str = "/source/trimmedia.db"
    snapshot_path: str = "/data/snapshot/origin.db"
    include_paths: list[str] = field(default_factory=list)
    exclude_paths: list[str] = field(default_factory=list)
    skip_season_zero: bool = True
    min_episodes: int = 1
    fnos: FnosOptions = field(default_factory=FnosOptions)


@dataclass(slots=True)
class SyncConfig:
    interval_seconds: int = 300
    watch_ratio: float = 0.9
    min_position_seconds: int = 60
    mark_in_progress: bool = True
    mark_completed: bool = True
    verify_remote: bool = True


@dataclass(slots=True)
class AccountConfig:
    name: str
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8765/oauth/callback"


@dataclass(slots=True)
class TargetConfig:
    type: str = "bangumi"
    user_agent: str = "bangumi-syncer/0.1.0 (+https://github.com/wwb051221/Bangumi-syncer)"
    timeout_seconds: int = 20
    max_retries: int = 3
    min_interval_seconds: float = 1.0
    accounts: list[AccountConfig] = field(default_factory=list)


@dataclass(slots=True)
class UserConfig:
    fnos_user: str
    account: str
    enabled: bool = True
    watch_ratio: float | None = None
    include_paths: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MatchConfig:
    min_score: float = 0.86
    min_margin: float = 0.06
    search_limit: int = 8
    accept_types: list[int] = field(default_factory=lambda: [2])  # 2 = anime
    auto_confirm: bool = False


@dataclass(slots=True)
class ServerConfig:
    enabled: bool = True
    listen: str = "0.0.0.0:8099"

    def host_port(self) -> tuple[str, int]:
        raw = self.listen.strip()
        if raw.startswith("["):  # [::]:8099
            host, _, port = raw[1:].partition("]:")
        else:
            host, _, port = raw.rpartition(":")
        if not host:
            host = "0.0.0.0"
        try:
            return host, int(port or "8099")
        except ValueError as exc:
            raise ConfigError("server.listen must look like '0.0.0.0:8099': %r" % self.listen) from exc


@dataclass(slots=True)
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    match: MatchConfig = field(default_factory=MatchConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    users: list[UserConfig] = field(default_factory=list)
    path: str | None = None

    # ------------------------------------------------------------- helpers
    def account(self, name: str) -> AccountConfig:
        for acc in self.target.accounts:
            if acc.name == name:
                return acc
        raise ConfigError("unknown account %r (defined: %s)" % (name, ", ".join(a.name for a in self.target.accounts)))

    def enabled_users(self) -> list[UserConfig]:
        return [u for u in self.users if u.enabled]

    def redacted(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("path", None)
        for acc in data.get("target", {}).get("accounts", []):
            if acc.get("client_secret"):
                acc["client_secret"] = "***"
        return data

    def validate(self) -> None:
        if self.source.type not in BUILTIN_SOURCE_TYPES:
            raise ConfigError(
                "source.type %r is not available (built-in: %s)"
                % (self.source.type, ", ".join(BUILTIN_SOURCE_TYPES))
            )
        if self.target.type not in BUILTIN_TARGET_TYPES:
            raise ConfigError(
                "target.type %r is not available (built-in: %s)"
                % (self.target.type, ", ".join(BUILTIN_TARGET_TYPES))
            )
        if self.source.mode not in ("live", "snapshot"):
            raise ConfigError("source.mode must be 'live' or 'snapshot', got %r" % self.source.mode)
        if not self.target.accounts:
            raise ConfigError("at least one [[accounts]] entry is required")
        names = [a.name for a in self.target.accounts]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ConfigError("duplicate account name(s): %s" % ", ".join(sorted(dupes)))
        if not self.users:
            raise ConfigError("at least one [[users]] entry is required")
        for user in self.users:
            if user.account not in names:
                raise ConfigError(
                    "user %r references unknown account %r (defined: %s)"
                    % (user.fnos_user, user.account, ", ".join(names))
                )
        if not 0.0 <= self.sync.watch_ratio <= 1.0:
            raise ConfigError("sync.watch_ratio must be between 0 and 1")
        if self.sync.interval_seconds < 30:
            raise ConfigError("sync.interval_seconds below 30s is not polite; use >= 30")
        if not 0.0 <= self.match.min_score <= 1.0:
            raise ConfigError("match.min_score must be between 0 and 1")
        if self.match.min_margin < 0:
            raise ConfigError("match.min_margin must be >= 0")
        self.server.host_port()
        self.source.include_paths = [_norm_path(p) for p in self.source.include_paths if p.strip()]
        self.source.exclude_paths = [_norm_path(p) for p in self.source.exclude_paths if p.strip()]
        for user in self.users:
            user.include_paths = [_norm_path(p) for p in user.include_paths if p.strip()]
            if user.watch_ratio is not None and not 0.0 <= user.watch_ratio <= 1.0:
                raise ConfigError("user %r watch_ratio must be between 0 and 1" % user.fnos_user)


# ------------------------------------------------------------------ parsing
def _norm_path(value: str) -> str:
    path = value.strip().replace("\\", "/")
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/")


def _expand_env(value: Any, missing: set[str]) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            env = os.environ.get(name)
            if env is None:
                if default is not None:
                    return default
                missing.add(name)
                return ""
            return env

        return ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(v, missing) for v in value]
    if isinstance(value, dict):
        return {k: _expand_env(v, missing) for k, v in value.items()}
    return value


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError("[%s] must be a table" % name)
    return value


def _build(klass: type, raw: dict[str, Any], where: str) -> Any:
    """Instantiate a dataclass from a TOML table, ignoring unknown keys."""
    import dataclasses

    fields = {f.name: f for f in dataclasses.fields(klass)}
    unknown = sorted(set(raw) - set(fields))
    if unknown:
        log.warning("ignoring unknown config key(s) in [%s]: %s", where, ", ".join(unknown))
    kwargs: dict[str, Any] = {}
    for name, f in fields.items():
        if name not in raw:
            continue
        value = raw[name]
        default = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default
        if isinstance(default, (int, float)) and not isinstance(default, bool) and isinstance(value, bool):
            raise ConfigError("%s.%s has wrong type: expected number, got bool" % (where, name))
        kwargs[name] = value
    return klass(**kwargs)


def loads(text: str, path: str | None = None) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("invalid TOML%s: %s" % (" in " + path if path else "", exc)) from exc

    missing: set[str] = set()
    data = _expand_env(data, missing)
    if missing:
        log.warning("environment variable(s) not set, expanded to empty: %s", ", ".join(sorted(missing)))

    users: list[UserConfig] = []
    raw_users = data.get("users", [])
    if not isinstance(raw_users, list):
        raise ConfigError("[[users]] must be an array of tables")
    for i, raw in enumerate(raw_users):
        if not isinstance(raw, dict):
            raise ConfigError("users[%d] must be a table" % i)
        if not raw.get("fnos_user") or not raw.get("account"):
            raise ConfigError("users[%d] needs both fnos_user and account" % i)
        users.append(_build(UserConfig, raw, "users[%d]" % i))

    target_raw = _section(data, "target")
    accounts: list[AccountConfig] = []
    raw_accounts = target_raw.pop("accounts", [])
    if not isinstance(raw_accounts, list):
        raise ConfigError("[[target.accounts]] must be an array of tables")
    for i, raw in enumerate(raw_accounts):
        if not isinstance(raw, dict) or not raw.get("name"):
            raise ConfigError("target.accounts[%d] needs a name" % i)
        accounts.append(_build(AccountConfig, raw, "target.accounts[%d]" % i))
    target = _build(TargetConfig, target_raw, "target")
    target.accounts = accounts

    source_raw = _section(data, "source")
    fnos_raw = source_raw.pop("fnos", {})
    source = _build(SourceConfig, source_raw, "source")
    source.fnos = _build(FnosOptions, fnos_raw, "source.fnos")

    cfg = Config(
        app=_build(AppConfig, _section(data, "app"), "app"),
        source=source,
        sync=_build(SyncConfig, _section(data, "sync"), "sync"),
        target=target,
        match=_build(MatchConfig, _section(data, "match"), "match"),
        server=_build(ServerConfig, _section(data, "server"), "server"),
        users=users,
        path=path,
    )
    cfg.validate()
    return cfg


def load(path: str | None = None) -> Config:
    candidates = [path] if path else [os.environ.get("BANGUMI_SYNCER_CONFIG", "")] + list(DEFAULT_CONFIG_PATHS)
    for candidate in candidates:
        if not candidate:
            continue
        if os.path.isfile(candidate):
            with open(candidate, "rb") as fh:
                # utf-8-sig: tolerate a BOM, editors on Windows like to add one.
                text = fh.read().decode("utf-8-sig")
            log.debug("loaded config from %s", candidate)
            return loads(text, candidate)
        if path:
            raise ConfigError("config file not found: %s" % candidate)
    raise ConfigError(
        "no config file found; looked in %s. Pass --config PATH or set BANGUMI_SYNCER_CONFIG."
        % ", ".join(c for c in candidates if c)
    )
