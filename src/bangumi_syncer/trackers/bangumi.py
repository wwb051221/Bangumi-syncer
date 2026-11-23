"""Bangumi (bgm.tv) tracker.

API notes (verified against api.bgm.tv in Nov 2026):

* ``GET  /v0/episodes?subject_id=..&type=0``              main episodes, paginated
* ``POST /v0/search/subjects``                            ``{"keyword":..,"filter":{"type":[2]}}``
* ``GET  /v0/subjects/{id}``                              subject detail (name, infobox)
* ``GET  /v0/users/-/collections/{id}``                   404 when not collected
* ``POST /v0/users/-/collections/{id}``                   create collection
* ``PATCH /v0/users/-/collections/{id}``                  update collection
* ``PUT  /v0/users/-/collections/{id}/episodes/{ep}``     mark one episode
* ``GET  /v0/users/-/collections/{id}/episodes``          episode marks (paginated)

Every request must carry a descriptive ``User-Agent``. Access tokens expire
after ~7 days; the refresh token is used automatically.
"""

from __future__ import annotations

import time
from datetime import date

from ..config import AccountConfig, TargetConfig
from ..http import ApiError, HttpClient
from ..log import get_logger
from ..state import StateStore, Tokens
from .base import (
    EP_COLLECTED,
    AuthError,
    SubjectHit,
    TrackEpisode,
    Tracker,
)

log = get_logger("tracker.bangumi")

API_BASE = "https://api.bgm.tv"
TOKEN_URL = "https://bgm.tv/oauth/access_token"
AUTHORIZE_URL = "https://bgm.tv/oauth/authorize"


class BangumiTracker(Tracker):
    name = "bangumi"

    def __init__(
        self,
        account: AccountConfig,
        target: TargetConfig,
        store: StateStore,
        http: HttpClient | None = None,
    ) -> None:
        self.account_cfg = account
        self.account = account.name
        self.target = target
        self.store = store
        self.http = http or HttpClient(
            user_agent=target.user_agent,
            timeout=target.timeout_seconds,
            max_retries=target.max_retries,
            min_interval=target.min_interval_seconds,
        )
        self._remote_user: str | None = None

    # ---------------------------------------------------------------- tokens
    def _tokens(self) -> Tokens:
        tokens = self.store.tokens(self.account)
        if tokens is None or not tokens.access_token:
            raise AuthError(
                'no Bangumi token for account %r. Run: bangumi-syncer oauth --account %s'
                % (self.account, self.account)
            )
        return tokens

    def _refresh(self, tokens: Tokens) -> Tokens:
        if not tokens.refresh_token:
            raise AuthError(
                "Bangumi refresh token missing for account %r; re-run: bangumi-syncer oauth --account %s"
                % (self.account, self.account)
            )
        log.info("refreshing Bangumi access token for %r", self.account)
        try:
            resp = self.http.request(
                "POST",
                TOKEN_URL,
                form_body={
                    "grant_type": "refresh_token",
                    "client_id": self.account_cfg.client_id,
                    "client_secret": self.account_cfg.client_secret,
                    "refresh_token": tokens.refresh_token,
                },
                max_retries=1,
            )
        except ApiError as exc:
            raise AuthError(
                "Bangumi token refresh rejected (%s). Run: bangumi-syncer oauth --account %s"
                % (exc.status, self.account)
            ) from exc
        return self._store_token_payload(resp.json(), fallback_refresh=tokens.refresh_token)

    def _store_token_payload(self, payload: object, fallback_refresh: str = "") -> Tokens:
        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise AuthError("unexpected Bangumi token response: %r" % (payload,))
        expires_in = int(payload.get("expires_in") or 604800)
        tokens = Tokens(
            access_token=str(payload["access_token"]),
            refresh_token=str(payload.get("refresh_token") or fallback_refresh),
            expires_at=int(time.time()) + expires_in,
            remote_user=str(payload.get("user_id") or "") or None,
        )
        self.store.save_tokens(self.account, tokens)
        return tokens

    def ensure_ready(self) -> None:
        tokens = self._tokens()
        if tokens.needs_refresh():
            tokens = self._refresh(tokens)
        self._remote_user = tokens.remote_user or self._remote_user
        try:
            me = self._request("GET", "/v0/me", max_retries=0)
            if isinstance(me, dict):
                self._remote_user = me.get("username") or self._remote_user
        except ApiError as exc:
            if exc.status in (401, 403):
                raise AuthError(
                    "Bangumi rejected the token for account %r. Re-run: bangumi-syncer oauth --account %s"
                    % (self.account, self.account)
                ) from exc
            log.debug("/v0/me unavailable (%s), continuing", exc.status)

    # --------------------------------------------------------------- request
    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        max_retries: int | None = None,
        allow_404: bool = False,
        auth_required: bool = True,
    ) -> object | None:
        url = API_BASE + path
        headers: dict[str, str] = {}
        if auth_required:
            tokens = self._tokens()
            if tokens.needs_refresh():
                tokens = self._refresh(tokens)
            headers["Authorization"] = "Bearer " + tokens.access_token
        try:
            resp = self.http.request(method, url, json_body=json_body, headers=headers,
                                     max_retries=max_retries)
            return resp.json() if resp.body else None
        except ApiError as exc:
            if exc.status == 401 and auth_required:
                # One refresh attempt then a single retry.
                tokens = self._refresh(self._tokens())
                resp = self.http.request(method, url, json_body=json_body,
                                         headers={"Authorization": "Bearer " + tokens.access_token},
                                         max_retries=0)
                return resp.json() if resp.body else None
            if exc.status == 404 and allow_404:
                return None
            raise

    # ----------------------------------------------------------------- calls
    def search(self, keyword: str, limit: int = 8, types: list[int] | None = None) -> list[SubjectHit]:
        body = {"keyword": keyword, "filter": {"type": types or [2]}}
        data = self._request(
            "POST", "/v0/search/subjects?limit=%d" % max(1, limit), json_body=body, auth_required=False
        )
        hits: list[SubjectHit] = []
        for item in (data or {}).get("data") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            hits.append(
                SubjectHit(
                    subject_id=int(item["id"]),
                    name=str(item.get("name") or ""),
                    name_cn=str(item.get("name_cn") or ""),
                    year=_year(item.get("date")),
                    episode_count=_int(item.get("eps")),
                    type=int(item.get("type") or 2),
                    score=_float((item.get("rating") or {}).get("score") if isinstance(item.get("rating"), dict) else item.get("score")),
                )
            )
        return hits

    def episodes(self, subject_id: int) -> list[TrackEpisode]:
        out: list[TrackEpisode] = []
        offset = 0
        while True:
            path = "/v0/episodes?subject_id=%d&type=0&limit=100&offset=%d" % (subject_id, offset)
            data = self._request("GET", path, auth_required=False) or {}
            rows = data.get("data") if isinstance(data, dict) else data
            if not rows:
                break
            for item in rows:
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                number = _float(item.get("sort"))
                if number is None:
                    number = _float(item.get("ep"))
                out.append(
                    TrackEpisode(
                        episode_id=int(item["id"]),
                        number=float(number if number is not None else len(out) + 1),
                        name=str(item.get("name") or ""),
                        name_cn=str(item.get("name_cn") or ""),
                        airdate=str(item.get("airdate") or "") or None,
                    )
                )
            offset += len(rows)
            total = _int(data.get("total")) if isinstance(data, dict) else None
            if total is not None and offset >= total:
                break
            if len(rows) < 100:
                break
        return out

    def subject_name(self, subject_id: int) -> str | None:
        data = self._request("GET", "/v0/subjects/%d" % subject_id, allow_404=True, auth_required=False)
        if not isinstance(data, dict):
            return None
        return str(data.get("name_cn") or data.get("name") or "") or None

    def collection(self, subject_id: int) -> int | None:
        data = self._request("GET", "/v0/users/-/collections/%d" % subject_id, allow_404=True)
        if not isinstance(data, dict):
            return None
        return _int(data.get("type"))

    def set_collection(self, subject_id: int, status: int) -> None:
        current = self.collection(subject_id)
        if current is None:
            self._request("POST", "/v0/users/-/collections/%d" % subject_id,
                          json_body={"type": status, "comment": ""})
        elif current != status:
            self._request("PATCH", "/v0/users/-/collections/%d" % subject_id,
                          json_body={"type": status})
        else:
            log.debug("subject %d already in collection type %d", subject_id, status)

    def episode_marks(self, subject_id: int) -> dict[int, int]:
        marks: dict[int, int] = {}
        offset = 0
        while True:
            path = "/v0/users/-/collections/%d/episodes?episode_type=0&limit=100&offset=%d" % (
                subject_id,
                offset,
            )
            data = self._request("GET", path, allow_404=True)
            rows = data.get("data") if isinstance(data, dict) else data
            if not rows:
                break
            for item in rows:
                if not isinstance(item, dict):
                    continue
                episode = item.get("episode") if isinstance(item.get("episode"), dict) else item
                episode_id = _int(episode.get("id")) if isinstance(episode, dict) else None
                status = _int(item.get("type"))
                if episode_id is not None and status is not None:
                    marks[episode_id] = status
            offset += len(rows)
            total = _int(data.get("total")) if isinstance(data, dict) else None
            if total is not None and offset >= total:
                break
            if len(rows) < 100:
                break
        return marks

    def mark_episode(self, subject_id: int, episode_id: int, status: int = EP_COLLECTED) -> None:
        # Bangumi v0 updates a single episode mark via the account-wide path.
        # The old /collections/{subject_id}/episodes/{episode_id} form returns 404.
        self._request(
            "PUT",
            "/v0/users/-/collections/-/episodes/%d" % episode_id,
            json_body={"type": int(status)},
        )

    def describe(self) -> dict[str, object]:
        return {
            "type": self.name,
            "account": self.account,
            "remote_user": self._remote_user,
        }


def _int(value: object) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _year(value: object) -> int | None:
    if not value:
        return None
    text = str(value)
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


def parse_airdate(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
