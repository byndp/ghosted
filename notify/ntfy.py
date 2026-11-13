"""ntfy adapter: one JSON publish per push (the default channel for people without the Worker).

Request: ``POST https://<server>/`` with ``{"topic","message","title","priority","tags","click"}``
(JSON, so UTF-8 titles survive; header values would go out as Latin-1). Facts behind the choices
(fact-checked 2026-10-05):

- Always send a title, or iOS shows ``ntfy.sh/<topic>``. Priority 4 for the dead-man (and any
  ``priority="high"`` push), else 3. Tags that are emoji short codes are prepended to the title,
  so only the dead-man gets one (``warning``).
- https only: ntfy.sh also answers plain http without redirecting, which would send the topic
  (the password of a public topic) in cleartext. http:// is allowed for localhost / 127.0.0.1.
- Never retry a 4xx: ntfy.sh's ban feed counts rejections per IP and feeds fail2ban, and 42908
  (daily quota) lasts until 00:00 UTC. A network error or a 5xx gets one retry after ~1 s.
- A 200 whose body is not ntfy's JSON is a captive portal or a proxy page, not a delivery.
- Bodies stay short (<= 1 KiB): iOS can drop payloads near 4 KB. Payloads use public titles only.
- ``NTFY_TOKEN`` (self-hosted servers) goes in ``Authorization: Bearer``. The topic and the token
  never appear in errors or ``check()`` messages.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from notify import base
from notify.base import SendResult

NAME = "ntfy"
DEFAULT_SERVER = "https://ntfy.sh"
TIMEOUT = 10.0
RETRY_DELAY = 1.0
MESSAGE_MAX_BYTES = 1024
TITLE_MAX_BYTES = 256
MAX_RESPONSE_BYTES = 64 * 1024
TOPIC_RE = re.compile(r"^[-_A-Za-z0-9]{1,64}$")
QUOTA_CODE = 42908  # daily message quota; resets at 00:00 UTC
PRIORITY_HIGH = 4
PRIORITY_DEFAULT = 3
_KIND_TAG = re.compile(r"^[a-z0-9_-]{1,32}$")


class _Outcome:
    __slots__ = ("ok", "status", "error", "retryable")

    def __init__(self, ok: bool, status: Optional[int], error: Optional[str], retryable: bool) -> None:
        self.ok, self.status, self.error, self.retryable = ok, status, error, retryable


def priority_for(kind: str, priority: str = "normal") -> int:
    """4 for the dead-man or an explicit high priority, else 3."""
    return PRIORITY_HIGH if kind == "deadman" or priority == "high" else PRIORITY_DEFAULT


def tags_for(kind: str) -> List[str]:
    """The dead-man gets the 'warning' emoji tag; every push carries its kind as a plain tag."""
    tags = [kind] if isinstance(kind, str) and _KIND_TAG.match(kind) else []
    return (["warning"] + tags) if kind == "deadman" else tags


class NtfyAdapter(base.Adapter):
    name = NAME
    owns_schedule = False

    def __init__(self, cfg: Any, env: Optional[Mapping[str, str]] = None, *,
                 timeout: float = TIMEOUT, retry_delay: float = RETRY_DELAY,
                 sleep: Callable[[float], None] = time.sleep,
                 transport: Optional[Callable[..., base.HttpResponse]] = None,
                 probe: Optional[Callable[[str, int, float], None]] = None,
                 reach_wait: float = base.REACH_WAIT) -> None:
        self.cfg = cfg
        env = dict(env or {})
        server = getattr(cfg, "ntfy_server", "") or DEFAULT_SERVER
        self.server = server.strip() if isinstance(server, str) else server
        topic = getattr(cfg, "ntfy_topic", "") or ""
        self.topic = topic.strip() if isinstance(topic, str) else ""
        self.token = env.get("NTFY_TOKEN") or None
        self.timeout = float(timeout)
        self.retry_delay = float(retry_delay)
        self.sleep = sleep
        self._transport = transport
        self._probe = probe
        self.reach_wait = float(reach_wait)

    def reachable(self) -> Tuple[bool, Optional[str]]:
        """Before ghosted run claims a push: can the server be reached at all (base.probe)? A
        configuration problem answers True, so send() reports and records it as before; with
        an injected transport (tests) and no injected probe there is nothing real to probe."""
        if self._probe is None and self._transport is not None:
            return True, None
        where, problem = self._target()
        if problem:
            return True, None
        ok, why = base.probe(self.server, wait=self.reach_wait, connect=self._probe, sleep=self.sleep)
        return (True, None) if ok else (False, "ntfy %s unreachable: %s" % (where, why))

    # -- config ------------------------------------------------------------------------------------

    def _target(self) -> Tuple[Optional[str], Optional[str]]:
        """(display origin, None) or (None, problem). Never echoes the topic or the token."""
        parts, problem = base.parse_url(self.server, "notify.ntfy.server")
        if problem:
            return None, problem
        assert parts is not None
        if parts.path not in ("", "/"):
            return None, "notify.ntfy.server must be the server's root address (no path)"
        if not self.topic:
            return None, "notify.ntfy.topic is not set (ghosted init writes a random one)"
        if not TOPIC_RE.match(self.topic):
            return None, "notify.ntfy.topic must be 1 to 64 letters, digits, '-' or '_'"
        return base.origin(parts), None

    def _redact(self) -> Tuple[Optional[str], ...]:
        return (self.topic, self.token)

    def _request(self, url: str, **kw: Any) -> base.HttpResponse:
        transport = self._transport or base.http_request
        return transport(url, timeout=self.timeout, max_bytes=MAX_RESPONSE_BYTES, **kw)

    # -- send --------------------------------------------------------------------------------------

    def payload(self, items: Any, *, title: Optional[str], body: Optional[str], kind: str,
                priority: str = "normal", click_url: Optional[str] = None) -> Dict[str, Any]:
        """The JSON publish body (public titles only)."""
        rows = base.iter_items(items)
        head = base.cap_utf8(base.push_title(title), TITLE_MAX_BYTES)
        out: Dict[str, Any] = {
            "topic": self.topic,
            "message": base.cap_utf8(base.push_body(rows, body, head), MESSAGE_MAX_BYTES),
            "title": head,
            "priority": priority_for(kind, priority),
            "tags": tags_for(kind),
        }
        if base.is_web_url(click_url):
            out["click"] = click_url
        return out

    def send(self, items: Any, *, title: Optional[str], body: Optional[str], kind: str,
             priority: str = "normal", click_url: Optional[str] = None) -> SendResult:
        where, problem = self._target()
        if problem:
            return SendResult(False, NAME, problem)
        rows = base.iter_items(items)
        head = base.push_title(title)  # tested before any cut, so a cut can't hide part of one
        leak = base.local_title_leak(rows, head, base.push_body(rows, body, head), click_url)
        if leak:
            return SendResult(False, NAME, leak)
        payload = self.payload(rows, title=title, body=body, kind=kind, priority=priority,
                               click_url=click_url)
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        url = self.server.rstrip("/") + "/"
        outcome = self._attempt(url, data, headers, where or "")
        if outcome.retryable:
            self.sleep(self.retry_delay)
            outcome = self._attempt(url, data, headers, where or "")
        return SendResult(outcome.ok, NAME, outcome.error, outcome.status)

    def _attempt(self, url: str, data: bytes, headers: Dict[str, str], where: str) -> _Outcome:
        try:
            resp = self._request(url, method="POST", body=data, headers=headers)
        except base.TransportError as exc:
            return _Outcome(False, None, "ntfy %s unreachable: %s" % (where, exc), exc.retryable)
        status = resp.status
        if 200 <= status < 300:
            obj = resp.json()
            if obj is None:
                return _Outcome(False, status, "ntfy %s answered with a page that is not ntfy's JSON "
                                               "(captive portal or proxy?)" % where, False)
            if not obj.get("id"):
                return _Outcome(False, status, "ntfy %s returned an unexpected response" % where, False)
            return _Outcome(True, status, None, False)
        if 300 <= status < 400:
            return _Outcome(False, status, "ntfy %s redirected (HTTP %d); set notify.ntfy.server to "
                                           "the final https:// address" % (where, status), False)
        return _Outcome(False, status, "ntfy " + self._http_error(resp), status >= 500)

    def _http_error(self, resp: base.HttpResponse) -> str:
        """'HTTP 429: limit reached: daily message quota reached (code 42908); ...'."""
        text = "HTTP %d" % resp.status
        obj = resp.json() or {}
        code = obj.get("code")
        detail = base.safe_text(obj.get("error"), redact=self._redact())
        if detail:
            text += ": " + detail
        if isinstance(code, int) and not isinstance(code, bool):
            text += " (code %d)" % code
            if code == QUOTA_CODE:
                text += "; the daily quota resets at 00:00 UTC"
        return text

    # -- doctor ------------------------------------------------------------------------------------

    def check(self) -> Tuple[bool, str]:
        where, problem = self._target()
        if problem:
            return False, "ntfy: " + problem
        notes = "topic set" + (", NTFY_TOKEN set" if self.token else "")
        url = self.server.rstrip("/") + "/v1/health"
        try:
            resp = self._request(url, method="GET", headers={"Accept": "application/json"})
        except base.TransportError as exc:
            return False, "ntfy %s unreachable: %s" % (where, exc)
        if resp.status == 200:
            obj = resp.json()
            if obj is None:
                return False, ("ntfy %s answered with a page that is not ntfy's JSON (captive portal "
                               "or proxy?)" % where)
            if obj.get("healthy") is True:
                return True, "ntfy %s reachable; %s" % (where, notes)
            return False, "ntfy %s reports that it is not healthy" % where
        if resp.status == 404:  # servers older than the health endpoint
            return True, "ntfy %s reachable (no health endpoint); %s" % (where, notes)
        if 300 <= resp.status < 400:
            return False, ("ntfy %s redirected (HTTP %d); set notify.ntfy.server to the final https:// "
                           "address" % (where, resp.status))
        return False, "ntfy %s: %s" % (where, self._http_error(resp))
