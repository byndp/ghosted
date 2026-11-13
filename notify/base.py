"""What every notify adapter shares: the Adapter interface, the result types, and a small HTTP
client that never follows redirects.

Adapters (contract sections 2, 12, 14):
- ``send(items, *, title, body, kind, priority="normal", click_url=None) -> SendResult`` pushes one
  notification. ``items`` are the ledger items the push is about; only their public ``title`` is
  ever used. ``local_title`` (Mac only) and transcript text never go into a request.
- ``check() -> (ok, message)`` for ``ghosted doctor``. Messages never contain secrets (tokens, the ntfy
  topic, service-token values) or response bodies.
- ``owns_schedule`` is True only for the remote adapter, whose Worker decides the pushes; for the
  local adapters ``ghosted run`` calls ``core.schedule.decide`` and then ``send``.

The HTTP client (``http_request``) returns every HTTP status as an ``HttpResponse`` (3xx included:
redirects are never followed, so a POST can't silently turn into a GET of a sign-in page), and
raises ``TransportError`` with a short reason when no response arrived. Requests to localhost /
127.0.0.1 bypass any configured proxy. Nothing here logs; error texts carry status codes, short
reasons and at most a short, filtered server error string.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import ssl
except ImportError:  # pragma: no cover - every supported Python has ssl
    ssl = None  # type: ignore[assignment]

from core import __version__

__all__ = [
    "Adapter", "SendResult", "SyncResult", "HttpResponse", "TransportError", "http_request",
    "describe_network_error", "parse_url", "origin", "safe_text", "cap_chars", "cap_utf8",
    "iter_items", "public_titles", "push_body", "push_title", "local_title_leak",
    "is_web_url", "USER_AGENT", "LOOPBACK_HOSTS", "DEFAULT_TITLE", "probe",
]

USER_AGENT = "ghosted/%s" % __version__
LOOPBACK_HOSTS = ("localhost", "127.0.0.1")  # the only hosts plain http:// may reach
DEFAULT_TITLE = "Ghosted"
MAX_ERROR_TEXT = 120
LOCAL_TITLE_MIN = 8  # shorter local titles are too generic to test for (false refusals)

# Fixed product strings that may appear in any push without counting as a leak.
_FIXED_TEXTS = ("Ghosted", "Due in 24 h", "Mac stopped reporting")


# ---- results ------------------------------------------------------------------------------------

@dataclass
class SendResult:
    """The outcome of one push. ``status`` is the HTTP status of the last attempt (None when no
    response arrived). ``error`` is a short, secret-free reason; on a remote test push that
    reached only some phones it may carry a note while ``ok`` is True."""

    ok: bool
    channel: str
    error: Optional[str] = None
    status: Optional[int] = None


@dataclass
class SyncResult:
    """The outcome of one ``RemoteAdapter.sync``.

    The first seven fields are the contract interface: ``acks`` maps event_id -> the server's ack
    ({result, item_id, rev, error}); ``stale`` lists (item_id, current server status) for every
    status change the server rejected as stale; ``merged`` counts local items added or changed by
    the server's view; ``test_push`` / ``pair`` echo the server's answers ({"sent","failed"},
    {"code","expires_at"}) or None. The rest is extra detail for ``ghosted sync`` and ``ghosted doctor``."""

    ok: bool
    error: Optional[str] = None
    acks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    stale: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    merged: int = 0
    test_push: Optional[Dict[str, Any]] = None
    pair: Optional[Dict[str, Any]] = None
    # -- additive detail (not part of the contract interface)
    invalid: List[Tuple[str, Optional[str], Optional[str]]] = field(default_factory=list)
    runs_acked: List[str] = field(default_factory=list)
    mirrored: int = 0
    requests: int = 0
    revoked: Optional[int] = None
    server_time: Optional[str] = None
    status: Optional[int] = None
    # (old id, new id) for items renumbered because the server held their id for another item
    renumbered: List[Tuple[str, str]] = field(default_factory=list)
    # ids this Mac believes synced that the server no longer has (`ghosted resync` sends them again)
    missing: List[str] = field(default_factory=list)
    # the Worker's push-channel status ({"subscriptions", "last_ok_at", "last_failure"}) or None
    push: Optional[Dict[str, Any]] = None


class Adapter:
    """Base class. Subclasses set ``name`` and implement ``send`` and ``check``."""

    name = "adapter"
    owns_schedule = False

    def send(self, items: Any, *, title: Optional[str], body: Optional[str], kind: str,
             priority: str = "normal", click_url: Optional[str] = None) -> SendResult:
        raise NotImplementedError

    def check(self) -> Tuple[bool, str]:
        raise NotImplementedError

    def __repr__(self) -> str:  # never shows configuration, which can hold the ntfy topic
        return "<%s adapter>" % self.name


# ---- HTTP ---------------------------------------------------------------------------------------

@dataclass
class HttpResponse:
    status: int
    headers: Dict[str, str]  # lower-case names
    body: bytes
    truncated: bool = False  # the body was longer than the caller's max_bytes

    def json(self) -> Optional[Dict[str, Any]]:
        """The body as a JSON object, or None (not JSON, not an object, or truncated)."""
        if self.truncated:
            return None
        try:
            obj = json.loads(self.body.decode("utf-8"))
        except ValueError:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
            return None
        return obj if isinstance(obj, dict) else None


class TransportError(Exception):
    """No HTTP response: DNS, refused, reset, TLS, timeout, or a request that could not be built.
    ``str(exc)`` is a short reason without URLs or secrets."""

    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Surface every 3xx as an HTTPError. Cloudflare Access answers a rejected service token with
    a redirect to its sign-in page; following it would turn the POST into a GET of HTML."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _opener(host: str) -> urllib.request.OpenerDirector:
    # Loopback never goes through a proxy (tests and the local Worker stay hermetic); anything
    # else honours the system / environment proxy settings as urllib normally does.
    proxies = urllib.request.ProxyHandler({}) if host in LOOPBACK_HOSTS else urllib.request.ProxyHandler()
    return urllib.request.build_opener(proxies, _NoRedirect())


def _lower_headers(message: Any) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        for key, value in message.items():
            out[str(key).lower()] = str(value)
    except Exception:  # a missing or odd header object
        pass
    return out


def http_request(url: str, *, method: str = "GET", body: Optional[bytes] = None,
                 headers: Optional[Mapping[str, str]] = None, timeout: float = 10.0,
                 max_bytes: int = 1 << 20) -> HttpResponse:
    """One request. Any HTTP status comes back as an HttpResponse (3xx are never followed); no
    response at all raises TransportError. At most ``max_bytes`` of the body are kept."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme.lower() not in ("http", "https") or not host:
            raise ValueError("only http(s) URLs")  # never file://, ftp:// or data: through here
        req = urllib.request.Request(url, data=body, method=method)
    except ValueError:
        raise TransportError("the request could not be built (bad URL)", retryable=False) from None
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if not req.has_header("User-agent"):
        req.add_header("User-Agent", USER_AGENT)
    try:
        resp = _opener(host).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:  # before OSError: HTTPError is a URLError
        return _from_http_error(exc, max_bytes)
    except http.client.InvalidURL:  # an HTTPException, but nothing was sent
        raise TransportError("the request could not be built (bad URL)", retryable=False) from None
    except (OSError, http.client.HTTPException) as exc:
        raise TransportError(describe_network_error(exc), retryable=True) from None
    except ValueError as exc:
        # A header value with a line break or a non-Latin-1 character (e.g. a mangled token) or
        # a malformed URL. The exception text can quote the header value, so only its type is used.
        raise TransportError("the request could not be built (%s)" % type(exc).__name__,
                             retryable=False) from None
    try:
        with resp:
            data = resp.read(max_bytes + 1)
            status = int(getattr(resp, "status", None) or resp.getcode())
            found = _lower_headers(resp.headers)
    except (OSError, http.client.HTTPException) as exc:
        raise TransportError(describe_network_error(exc), retryable=True) from None
    return HttpResponse(status, found, data[:max_bytes], truncated=len(data) > max_bytes)


REACH_WAIT = 30.0  # seconds a probe keeps trying: Wi-Fi is often still joining at a wake run
REACH_STEP = 5.0


def _proxy_for(scheme: str, host: str) -> Optional[Tuple[str, int]]:
    """(host, port) of the proxy urllib would use for this URL, or None for a direct one."""
    if host in LOOPBACK_HOSTS:
        return None
    try:
        if urllib.request.proxy_bypass(host):
            return None
        proxy = urllib.request.getproxies().get(scheme)
    except Exception:  # an odd system proxy configuration: probe directly
        return None
    if not proxy:
        return None
    parts = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    return parts.hostname, port or (443 if parts.scheme == "https" else 80)


def _tcp_connect(host: str, port: int, timeout: float) -> None:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.close()


def probe(url: str, *, wait: float = REACH_WAIT, step: float = REACH_STEP,
          connect: Optional[Callable[[str, int, float], None]] = None,
          sleep: Callable[[float], None] = time.sleep,
          clock: Callable[[], float] = time.monotonic) -> Tuple[bool, Optional[str]]:
    """Can a TCP connection to ``url``'s server (or the proxy urllib would use) be opened at all?
    Tries every ``step`` seconds for up to ``wait`` seconds. (True, None), or (False, a short
    reason such as "DNS lookup failed"). Sends nothing: a failure here proves no request was
    made, so the caller may try again later without risking a double push."""
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        scheme = parts.scheme.lower()
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return False, "bad URL"
    if not host:
        return False, "bad URL"
    target = _proxy_for(scheme, host) or (host, port)
    dial = connect or _tcp_connect
    deadline = clock() + max(0.0, wait)
    while True:
        try:
            dial(target[0], target[1], max(0.5, min(step, 10.0)))
            return True, None
        except Exception as exc:  # noqa: BLE001 - every failure to connect means "not reachable"
            reason = describe_network_error(exc) if isinstance(exc, BaseException) else "network error"
        if clock() + step > deadline:
            return False, reason
        sleep(step)


def _from_http_error(exc: urllib.error.HTTPError, max_bytes: int) -> HttpResponse:
    try:
        data = exc.read(max_bytes + 1) or b""
    except Exception:  # the body may be gone with the connection, or never existed
        data = b""
    found = _lower_headers(getattr(exc, "headers", None))
    try:
        exc.close()
    except Exception:
        pass
    return HttpResponse(int(exc.code), found, data[:max_bytes], truncated=len(data) > max_bytes)


def describe_network_error(exc: BaseException) -> str:
    """A short reason for a failed request: no URL, no headers, no body."""
    reason: Any = exc
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        reason = exc.reason
    if isinstance(reason, str):
        return safe_text(reason, 80) or "network error"
    if ssl is not None and isinstance(reason, ssl.SSLCertVerificationError):
        return ("TLS certificate not trusted (python.org builds of Python need "
                "'Install Certificates.command')")
    if ssl is not None and isinstance(reason, ssl.SSLError):
        return "TLS error"
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "timed out"
    if isinstance(reason, socket.gaierror):
        return "DNS lookup failed"
    if isinstance(reason, ConnectionRefusedError):
        return "connection refused"
    if isinstance(reason, http.client.RemoteDisconnected):
        return "connection closed without a response"
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "connection reset"
    if isinstance(reason, http.client.IncompleteRead):
        return "response cut off"
    if isinstance(reason, http.client.HTTPException):
        return "malformed HTTP response"
    if isinstance(reason, OSError):
        return safe_text(reason.strerror or "", 80) or type(reason).__name__
    return type(reason).__name__


# ---- URLs ---------------------------------------------------------------------------------------

def parse_url(url: Any, what: str) -> Tuple[Optional[urllib.parse.SplitResult], Optional[str]]:
    """Validate a configured base URL. https:// is required; http:// only for localhost and
    127.0.0.1 (tests, the local Worker). Returns (parts, None) or (None, problem). The problem
    text never repeats the URL (it could carry credentials)."""
    if not isinstance(url, str) or not url.strip():
        return None, "%s is not set" % what
    if any(ord(ch) <= 32 or ord(ch) == 127 for ch in url.strip()):
        return None, "%s must not contain spaces or control characters" % what
    try:
        parts = urllib.parse.urlsplit(url.strip())
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return None, "%s is not a valid URL" % what
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in ("https", "http") or not host:
        return None, "%s must be an https:// URL" % what
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None, "%s must not contain a user name or password" % what
    if scheme == "http" and host not in LOOPBACK_HOSTS:
        return None, "%s must use https:// (plain http:// is only for localhost)" % what
    if parts.query or parts.fragment:
        return None, "%s must not have a query string or fragment" % what
    return parts, None


def origin(parts: urllib.parse.SplitResult) -> str:
    """'https://host[:port]' for messages (no path, no credentials)."""
    host = (parts.hostname or "").lower()
    if ":" in host:  # IPv6 literal
        host = "[%s]" % host
    return "%s://%s%s" % (parts.scheme.lower(), host, ":%d" % parts.port if parts.port else "")


def is_web_url(value: Any, limit: int = 2048) -> bool:
    """True for an http(s) URL short enough to hand to a push service as a click target."""
    if not isinstance(value, str) or not value or len(value) > limit:
        return False
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        return False
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return parts.scheme.lower() in ("https", "http") and bool(parts.hostname)


# ---- text ---------------------------------------------------------------------------------------

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9 _.,:;'()/-]+")


def safe_text(value: Any, limit: int = MAX_ERROR_TEXT, redact: Sequence[Optional[str]] = ()) -> str:
    """A short, plain fragment of server-provided text for an error message: secrets in
    ``redact`` are blanked, anything outside a small character set becomes a space, the result
    is cut to ``limit`` characters. '' when nothing usable is left."""
    if not isinstance(value, str):
        return ""
    text = value
    for secret in redact:
        if secret:
            text = text.replace(secret, " ")
    text = " ".join(_UNSAFE_CHARS.sub(" ", text).split())
    return text[:limit].rstrip()


def cap_chars(text: str, limit: int, mark: str = "…") -> str:
    """At most ``limit`` code points; a cut text ends with ``mark``."""
    if len(text) <= limit:
        return text
    return text[: max(limit - len(mark), 0)] + mark


def cap_utf8(text: str, limit: int, mark: str = "…") -> str:
    """At most ``limit`` UTF-8 bytes, cut on a character boundary; a cut text ends with ``mark``."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    room = max(limit - len(mark.encode("utf-8")), 0)
    return data[:room].decode("utf-8", "ignore") + mark


# ---- push content -------------------------------------------------------------------------------

def iter_items(items: Any) -> List[Mapping[str, Any]]:
    """The item mappings of ``items`` (a list of items, or a dict of id -> item)."""
    if items is None:
        return []
    if isinstance(items, Mapping):
        items = list(items.values())
    try:
        rows = list(items)
    except TypeError:
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def public_titles(items: Iterable[Mapping[str, Any]]) -> List[str]:
    """The public titles, in order (never ``local_title``)."""
    out = []
    for item in items:
        title = item.get("title")
        if isinstance(title, str) and title.strip():
            out.append(title)
    return out


def push_title(title: Optional[str]) -> str:
    """Every push has a title (ntfy shows 'ntfy.sh/<topic>' otherwise)."""
    if isinstance(title, str) and title.strip():
        return " ".join(title.split())
    return DEFAULT_TITLE


def push_body(items: Sequence[Mapping[str, Any]], body: Optional[str], title: str) -> str:
    """The given body, or (when blank) the digest line built from the items' public titles."""
    if isinstance(body, str) and body.strip():
        return body
    titles = public_titles(items)
    if titles:
        from core.schedule import digest_body  # the same "{n} open: a · b · …" line the rules build
        return digest_body(titles)
    return title


def local_title_leak(items: Sequence[Mapping[str, Any]], *texts: Optional[str]) -> Optional[str]:
    """Defence in depth against a caller that built the push text from local titles.

    Returns a refusal reason when a pushed text still contains an item's ``local_title`` after
    every public title (and the fixed product strings) has been taken out of it, else None.
    Local titles that equal their public title, are shorter than LOCAL_TITLE_MIN, or are part of
    a public title in this push are public already and are not tested."""
    publics = public_titles(items)
    privates = []
    for item in items:
        local = item.get("local_title")
        if not isinstance(local, str) or len(local.strip()) < LOCAL_TITLE_MIN:
            continue
        if local == item.get("title") or any(local in pub for pub in publics):
            continue
        privates.append(local)
    if not privates:
        return None
    removable = sorted(set(publics) | set(_FIXED_TEXTS), key=len, reverse=True)
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        rest = text
        for pub in removable:
            rest = rest.replace(pub, "\x00")
        if any(local in rest for local in privates):
            return "refused: the push text contains a local (Mac-only) title"
    return None
