"""Pushover adapter: one form POST per push to https://api.pushover.net/1/messages.json.

Fields: ``token`` and ``user`` (``PUSHOVER_TOKEN`` / ``PUSHOVER_USER`` in .env), ``title`` (<= 250
characters), ``message`` (<= 1024 characters), ``url`` (<= 512 characters, when a click target
is given) and ``priority`` (0; 1 for the dead-man or any ``priority="high"`` push). Public titles
only, never local titles or transcript text.

Error handling follows Pushover's API guidance (pushover.net/api, "Being Friendly to our API"): a
4xx (including 429, the monthly quota) never succeeds on a repeat, and many 4xx get the IP blocked,
so it is never retried; a 5xx or no reply gets one retry, no sooner than 5 seconds later. A 200
must carry ``{"status": 1}``; anything else (or a page that is not JSON: a captive portal) is a
failure. Tokens and user keys never appear in errors or ``check()`` messages.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from notify import base
from notify.base import SendResult

NAME = "pushover"
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
TIMEOUT = 10.0
RETRY_DELAY = 5.0  # Pushover: repeat a failed request "no sooner than 5 seconds" later
MESSAGE_MAX = 1024
TITLE_MAX = 250
URL_MAX = 512
MAX_RESPONSE_BYTES = 64 * 1024


class _Outcome:
    __slots__ = ("ok", "status", "error", "retryable", "body")

    def __init__(self, ok: bool, status: Optional[int], error: Optional[str], retryable: bool,
                 body: Optional[Dict[str, Any]] = None) -> None:
        self.ok, self.status, self.error, self.retryable, self.body = ok, status, error, retryable, body


def priority_for(kind: str, priority: str = "normal") -> int:
    """1 (bypasses quiet hours) for the dead-man or an explicit high priority, else 0."""
    return 1 if kind == "deadman" or priority == "high" else 0


def validate_url_for(endpoint: str) -> str:
    """The users/validate.json endpoint next to a messages.json endpoint."""
    if endpoint.endswith("/messages.json"):
        return endpoint[: -len("/messages.json")] + "/users/validate.json"
    return endpoint.rstrip("/") + "/users/validate.json"


class PushoverAdapter(base.Adapter):
    name = NAME
    owns_schedule = False

    def __init__(self, cfg: Any, env: Optional[Mapping[str, str]] = None, *,
                 endpoint: str = PUSHOVER_URL, validate_endpoint: Optional[str] = None,
                 timeout: float = TIMEOUT, retry_delay: float = RETRY_DELAY,
                 sleep: Callable[[float], None] = time.sleep,
                 transport: Optional[Callable[..., base.HttpResponse]] = None,
                 probe: Optional[Callable[[str, int, float], None]] = None,
                 reach_wait: float = base.REACH_WAIT) -> None:
        self.cfg = cfg
        env = dict(env or {})
        self.token = env.get("PUSHOVER_TOKEN") or None
        self.user = env.get("PUSHOVER_USER") or None
        self.endpoint = endpoint
        self.validate_endpoint = validate_endpoint or validate_url_for(endpoint)
        self.timeout = float(timeout)
        self.retry_delay = float(retry_delay)
        self.sleep = sleep
        self._transport = transport
        self._probe = probe
        self.reach_wait = float(reach_wait)

    def reachable(self) -> Tuple[bool, Optional[str]]:
        """Before ghosted run claims a push: can Pushover be reached at all (base.probe)? See
        NtfyAdapter.reachable."""
        if self._probe is None and self._transport is not None:
            return True, None
        where, problem = self._target(self.endpoint)
        if problem:
            return True, None
        ok, why = base.probe(self.endpoint, wait=self.reach_wait, connect=self._probe, sleep=self.sleep)
        return (True, None) if ok else (False, "pushover %s unreachable: %s" % (where, why))

    # -- config ------------------------------------------------------------------------------------

    def _missing(self) -> List[str]:
        return [key for key, value in (("PUSHOVER_TOKEN", self.token), ("PUSHOVER_USER", self.user))
                if not value]

    def _target(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        missing = self._missing()
        if missing:
            return None, "%s missing from .env" % " and ".join(missing)
        parts, problem = base.parse_url(url, "the Pushover endpoint")
        if problem:
            return None, problem
        assert parts is not None
        return base.origin(parts), None

    def _redact(self) -> Tuple[Optional[str], ...]:
        return (self.token, self.user)

    def _request(self, url: str, **kw: Any) -> base.HttpResponse:
        transport = self._transport or base.http_request
        return transport(url, timeout=self.timeout, max_bytes=MAX_RESPONSE_BYTES, **kw)

    # -- send --------------------------------------------------------------------------------------

    def fields(self, items: Any, *, title: Optional[str], body: Optional[str], kind: str,
               priority: str = "normal", click_url: Optional[str] = None) -> Dict[str, str]:
        """The form fields (public titles only)."""
        rows = base.iter_items(items)
        head = base.cap_chars(base.push_title(title), TITLE_MAX)
        out = {
            "token": self.token or "",
            "user": self.user or "",
            "title": head,
            "message": base.cap_chars(base.push_body(rows, body, head), MESSAGE_MAX),
            "priority": str(priority_for(kind, priority)),
        }
        if base.is_web_url(click_url, limit=URL_MAX):
            out["url"] = click_url  # type: ignore[assignment]
        return out

    def send(self, items: Any, *, title: Optional[str], body: Optional[str], kind: str,
             priority: str = "normal", click_url: Optional[str] = None) -> SendResult:
        where, problem = self._target(self.endpoint)
        if problem:
            return SendResult(False, NAME, problem)
        rows = base.iter_items(items)
        head = base.push_title(title)
        leak = base.local_title_leak(rows, head, base.push_body(rows, body, head), click_url)
        if leak:
            return SendResult(False, NAME, leak)
        form = self.fields(rows, title=title, body=body, kind=kind, priority=priority,
                           click_url=click_url)
        data = urllib.parse.urlencode(form).encode("ascii")
        outcome = self._attempt(self.endpoint, data, where or "")
        if outcome.retryable:
            self.sleep(self.retry_delay)
            outcome = self._attempt(self.endpoint, data, where or "")
        return SendResult(outcome.ok, NAME, outcome.error, outcome.status)

    def _attempt(self, url: str, data: bytes, where: str, what: str = "the message") -> _Outcome:
        headers = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                   "Accept": "application/json"}
        try:
            resp = self._request(url, method="POST", body=data, headers=headers)
        except base.TransportError as exc:
            return _Outcome(False, None, "pushover %s unreachable: %s" % (where, exc), exc.retryable)
        status = resp.status
        obj = resp.json()
        if 200 <= status < 300:
            if obj is None:
                return _Outcome(False, status, "pushover %s answered with a page that is not JSON "
                                               "(captive portal or proxy?)" % where, False)
            if obj.get("status") != 1:
                return _Outcome(False, status, "pushover rejected %s%s" % (what, self._errors(obj)),
                                False, obj)
            return _Outcome(True, status, None, False, obj)
        if 300 <= status < 400:
            return _Outcome(False, status, "pushover %s redirected (HTTP %d)" % (where, status), False)
        text = "pushover HTTP %d" % status
        if status == 429:
            text += ": the monthly message limit is reached"
        text += self._errors(obj or {})
        return _Outcome(False, status, text, status >= 500, obj)

    def _errors(self, obj: Mapping[str, Any]) -> str:
        found = obj.get("errors")
        if not isinstance(found, list):
            return ""
        parts = [base.safe_text(e, 80, redact=self._redact()) for e in found[:3]]
        parts = [p for p in parts if p]
        return (": " + "; ".join(parts)) if parts else ""

    # -- doctor ------------------------------------------------------------------------------------

    def check(self) -> Tuple[bool, str]:
        """Validates the token and user key with users/validate.json (sends no message)."""
        where, problem = self._target(self.validate_endpoint)
        if problem:
            return False, "pushover: " + problem
        data = urllib.parse.urlencode({"token": self.token or "", "user": self.user or ""}).encode("ascii")
        outcome = self._attempt(self.validate_endpoint, data, where or "", what="the keys")
        if not outcome.ok:
            return False, outcome.error or "pushover: validation failed"
        devices = (outcome.body or {}).get("devices")
        count = len(devices) if isinstance(devices, list) else None
        if count is None:
            return True, "pushover keys valid"
        return True, "pushover keys valid; %d active device%s" % (count, "" if count == 1 else "s")
