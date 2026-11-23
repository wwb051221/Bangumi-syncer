"""Bangumi OAuth 2.0 authorization-code flow.

The refresh token is what makes unattended syncing possible: the access token
lives for a week, the refresh token is used transparently by the tracker.

Two ways to complete the flow:

* ``--manual``  print the authorize URL, then paste the ``code`` from the
  redirect URL back into the terminal (works over SSH, no port forwarding);
* default       start a tiny loopback HTTP server on the ``redirect_uri`` port
  and catch the code automatically.

Either way the exchanged tokens are stored in the state database.
"""

from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from .config import AccountConfig, TargetConfig
from .http import ApiError, HttpClient
from .log import get_logger
from .state import StateStore, Tokens

log = get_logger("oauth")

AUTHORIZE_URL = "https://bgm.tv/oauth/authorize"
TOKEN_URL = "https://bgm.tv/oauth/access_token"
DEFAULT_PORT = 8765

_SUCCESS_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>fnos2bgm</title></head>
<body style="font-family:system-ui,sans-serif;padding:3rem;max-width:38rem;margin:auto">
<h2>✅ fnos2bgm 授权完成</h2>
<p>Bangumi 授权已接收，可以关闭此页面回到终端。</p>
<p style="color:#666">Authorization received. You can close this tab.</p>
</body></html>"""


class OAuthError(Exception):
    pass


@dataclass(slots=True)
class CallbackResult:
    code: str | None = None
    error: str | None = None


def authorize_url(account: AccountConfig, state: str) -> str:
    params = {
        "client_id": account.client_id,
        "response_type": "code",
        "redirect_uri": account.redirect_uri,
        "state": state,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def exchange_code(account: AccountConfig, code: str, http: HttpClient) -> Tokens:
    if not account.client_id or not account.client_secret:
        raise OAuthError(
            "account %r needs client_id and client_secret from https://bgm.tv/dev/apps "
            "(set them in the config or via ${ENV_VAR})" % account.name
        )
    try:
        resp = http.request(
            "POST",
            TOKEN_URL,
            form_body={
                "grant_type": "authorization_code",
                "client_id": account.client_id,
                "client_secret": account.client_secret,
                "code": code,
                "redirect_uri": account.redirect_uri,
            },
            max_retries=1,
        )
    except ApiError as exc:
        raise OAuthError("token exchange failed (%s): %s" % (exc.status, exc.message)) from exc
    payload = resp.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise OAuthError("unexpected token response: %r" % (payload,))
    expires_in = int(payload.get("expires_in") or 604800)
    return Tokens(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_at=int(time.time()) + expires_in,
        remote_user=str(payload.get("user_id") or "") or None,
    )


class _CallbackHandler(BaseHTTPRequestHandler):
    expected_path = "/oauth/callback"
    expected_state = ""

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != self.expected_path:
            self.send_error(404)
            return
        params = urllib.parse.parse_qs(parsed.query)
        state = (params.get("state") or [""])[0]
        if self.expected_state and state and state != self.expected_state:
            self.send_error(400, "state mismatch")
            return
        result = CallbackResult(
            code=(params.get("code") or [None])[0],
            error=(params.get("error") or [None])[0],
        )
        self.server.callback_result = result  # type: ignore[attr-defined]
        body = _SUCCESS_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # silence access log
        return


def wait_for_code(account: AccountConfig, state: str, timeout: float = 300.0) -> str:
    redirect = urllib.parse.urlparse(account.redirect_uri)
    host = redirect.hostname or "127.0.0.1"
    port = redirect.port or DEFAULT_PORT
    path = redirect.path or "/oauth/callback"

    handler = type("BoundHandler", (_CallbackHandler,), {"expected_path": path, "expected_state": state})
    try:
        server = HTTPServer((host, port), handler)
    except OSError as exc:
        raise OAuthError(
            "cannot listen on %s:%d for the OAuth callback (%s). Use --manual instead."
            % (host, port, exc)
        ) from exc
    server.timeout = 1.0  # type: ignore[attr-defined]

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    log.info("waiting for the Bangumi callback on http://%s:%d%s (Ctrl-C to abort)", host, port, path)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            result = getattr(server, "callback_result", None)
            if result is not None:
                if result.error:
                    raise OAuthError("Bangumi returned an error: %s" % result.error)
                if not result.code:
                    raise OAuthError("callback did not contain a code")
                return result.code
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()
    raise OAuthError("timed out after %.0fs waiting for the OAuth callback" % timeout)


def login(
    account: AccountConfig,
    store: StateStore,
    target: TargetConfig,
    *,
    manual: bool = False,
    open_browser: bool = True,
    timeout: float = 300.0,
) -> Tokens:
    http = HttpClient(
        user_agent=target.user_agent,
        timeout=target.timeout_seconds,
        max_retries=target.max_retries,
        min_interval=0,
    )
    state = secrets.token_urlsafe(16)
    url = authorize_url(account, state)
    print("")
    print("1) Open this URL in a browser and approve access for Bangumi account %r:" % account.name)
    print("")
    print("   " + url)
    print("")
    if open_browser and not manual:
        try:
            webbrowser.open(url, new=2)
        except Exception:  # pragma: no cover - headless containers
            pass

    if manual:
        code = input("2) Paste the 'code' parameter from the redirect URL: ").strip()
        if not code:
            raise OAuthError("no code entered")
    else:
        print("2) Waiting for the redirect ... (use --manual if the browser is on another machine)")
        code = wait_for_code(account, state, timeout=timeout)

    tokens = exchange_code(account, code, http)
    store.save_tokens(account.name, tokens)
    print("")
    print("✅ stored tokens for %r (access token valid for ~7 days, auto-refreshed)" % account.name)

    remote = _remote_user(http, tokens)
    if remote:
        print("   Bangumi user: %s" % remote)
        tokens.remote_user = remote
        store.save_tokens(account.name, tokens)
    return tokens


def _remote_user(http: HttpClient, tokens: Tokens) -> str | None:
    try:
        resp = http.request(
            "GET",
            "https://api.bgm.tv/v0/me",
            headers={"Authorization": "Bearer " + tokens.access_token},
            max_retries=0,
        )
        data = resp.json()
        if isinstance(data, dict):
            return str(data.get("username") or data.get("nickname") or "") or None
    except ApiError as exc:
        log.debug("could not read /v0/me: %s", exc)
    return None
