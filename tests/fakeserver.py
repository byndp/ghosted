"""A small threaded HTTP server for tests: records every request and answers from a script.

    with FakeServer() as srv:
        srv.queue(Response(500), Response(200, json={"id": "abc"}))   # first two answers
        ... point the code under test at srv.url ...
        srv.requests[0].json()                                         # what it sent

Answers come from, in order: the queue (FIFO), then ``handler(request)`` if it returns a Response,
then a default 200 ``{}``. ``Response(drop=True)`` closes the connection without answering (a
network failure); ``Response(delay=s)`` answers late (a timeout). Binds 127.0.0.1 on a free port,
never touches the network, prints nothing (handler exceptions become a 500 and land in
``srv.errors``).
"""
from __future__ import annotations

import json as _json
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

__all__ = ["FakeServer", "Request", "Response", "closed_port"]


class Request:
    """One recorded request. Header names are lower-case."""

    def __init__(self, method: str, path: str, query: str, headers: Dict[str, str], body: bytes) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        return self.headers.get(name.lower(), default)

    def json(self) -> Any:
        return _json.loads(self.body.decode("utf-8"))

    def form(self) -> Dict[str, str]:
        """An application/x-www-form-urlencoded body (last value wins)."""
        pairs = urllib.parse.parse_qsl(self.body.decode("utf-8"), keep_blank_values=True)
        return dict(pairs)

    def __repr__(self) -> str:
        return "<Request %s %s (%d bytes)>" % (self.method, self.path, len(self.body))


class Response:
    def __init__(self, status: int = 200, json: Any = None, body: Optional[bytes] = None,
                 headers: Optional[Dict[str, str]] = None, content_type: Optional[str] = None,
                 drop: bool = False, delay: float = 0.0) -> None:
        self.status = status
        self.json = json
        self.body = body
        self.headers = dict(headers or {})
        self.content_type = content_type
        self.drop = drop
        self.delay = delay

    def payload(self) -> Tuple[bytes, str]:
        if self.body is not None:
            return self.body, self.content_type or "application/octet-stream"
        if self.json is not None:
            return _json.dumps(self.json).encode("utf-8"), self.content_type or "application/json"
        return b"", self.content_type or "text/plain"


Handler = Callable[[Request], Optional[Response]]


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False  # stop() must not wait for a deliberately slow handler
    allow_reuse_address = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a client that hung up (timeouts, drops) is expected here; stay quiet


class FakeServer:
    def __init__(self, handler: Optional[Handler] = None, host: str = "127.0.0.1") -> None:
        self.handler = handler
        self.host = host
        self.requests: List[Request] = []
        self.errors: List[BaseException] = []
        self._queue: List[Response] = []
        self._lock = threading.Lock()
        self._server: Optional[_Server] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------------------------------

    def start(self) -> "FakeServer":
        fake = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"  # one request per connection; simplest to reason about

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

            def _serve(self) -> None:
                fake._dispatch(self)

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _serve

        self._server = _Server((self.host, 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def __enter__(self) -> "FakeServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._server is not None, "server not started"
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        return "http://%s:%d" % (self.host, self.port)

    # -- scripting ---------------------------------------------------------------------------------

    def queue(self, *responses: Response) -> None:
        with self._lock:
            self._queue.extend(responses)

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()
            self._queue.clear()
            self.errors.clear()

    def bodies(self) -> List[bytes]:
        with self._lock:
            return [r.body for r in self.requests]

    def _next(self, req: Request) -> Response:
        with self._lock:
            if self._queue:
                return self._queue.pop(0)
        if self.handler is not None:
            try:
                answer = self.handler(req)
            except Exception as exc:  # surface handler bugs to the test, not to stderr
                with self._lock:
                    self.errors.append(exc)
                return Response(500, json={"error": "fake handler raised"})
            if answer is not None:
                return answer
        return Response(200, json={})

    def _dispatch(self, h: BaseHTTPRequestHandler) -> None:
        try:
            length = int(h.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = h.rfile.read(length) if length > 0 else b""
        parts = urllib.parse.urlsplit(h.path)
        req = Request(h.command, parts.path, parts.query,
                      {k.lower(): v for k, v in h.headers.items()}, body)
        with self._lock:
            self.requests.append(req)
        resp = self._next(req)
        if resp.delay:
            time.sleep(resp.delay)
        if resp.drop:
            h.close_connection = True
            try:
                h.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        data, ctype = resp.payload()
        h.send_response(resp.status)
        h.send_header("Content-Type", ctype)
        for key, value in resp.headers.items():
            h.send_header(key, value)
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        if h.command != "HEAD" and data:
            h.wfile.write(data)


def closed_port(host: str = "127.0.0.1") -> int:
    """A local port nothing listens on (bound, then released), for 'connection refused'."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()
