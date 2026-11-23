from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from bangumi_syncer.http import ApiError, HttpClient, NetworkError


class Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []


def serve(recorder: Recorder):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            recorder.requests.append(self.path)
            status, headers, body = recorder.responses.pop(0)
            payload = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, "http://127.0.0.1:%d" % server.server_port


@pytest.fixture()
def server_factory():
    servers = []

    def make(responses):
        recorder = Recorder(responses)
        server, url = serve(recorder)
        servers.append(server)
        return recorder, url

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


def test_json_request(server_factory):
    recorder, url = server_factory([(200, {"Content-Type": "application/json"}, {"ok": True})])
    client = HttpClient("bangumi-syncer-test/0", timeout=5, max_retries=0, min_interval=0)
    resp = client.request("GET", url + "/hello")
    assert resp.status == 200
    assert resp.json() == {"ok": True}
    assert recorder.requests == ["/hello"]


def test_retries_on_500(server_factory):
    recorder, url = server_factory([
        (500, {}, {"error": "boom"}),
        (200, {"Content-Type": "application/json"}, {"ok": 1}),
    ])
    client = HttpClient("bangumi-syncer-test/0", timeout=5, max_retries=1, min_interval=0)
    resp = client.request("GET", url + "/retry")
    assert resp.status == 200
    assert len(recorder.requests) == 2


def test_honours_retry_after(server_factory):
    recorder, url = server_factory([
        (429, {"Retry-After": "0.01"}, {"error": "slow down"}),
        (200, {"Content-Type": "application/json"}, {"ok": 1}),
    ])
    client = HttpClient("bangumi-syncer-test/0", timeout=5, max_retries=1, min_interval=0)
    assert client.request("GET", url + "/limited").status == 200
    assert len(recorder.requests) == 2


def test_client_error_is_not_retried(server_factory):
    recorder, url = server_factory([(400, {}, {"error": "bad request"})])
    client = HttpClient("bangumi-syncer-test/0", timeout=5, max_retries=3, min_interval=0)
    with pytest.raises(ApiError) as excinfo:
        client.request("GET", url + "/bad")
    assert excinfo.value.status == 400
    assert len(recorder.requests) == 1


def test_network_error(tmp_path):
    # nothing listens on port 1 on the loopback interface
    client = HttpClient("bangumi-syncer-test/0", timeout=2, max_retries=0, min_interval=0)
    with pytest.raises(NetworkError):
        client.request("GET", "http://127.0.0.1:1/nope")
