"""Remote adapter: the Mac's half of the sync with the Cloudflare Worker (contract sections 14, 15).

``RemoteAdapter.sync(ledger, now)`` POSTs ``<notify.remote.url>/api/sync``:

- **Auth.** ``CF-Access-Client-Id`` / ``CF-Access-Client-Secret`` (``GHOSTED_SERVICE_TOKEN_ID`` /
  ``GHOSTED_SERVICE_TOKEN_SECRET`` from .env) on every request. Redirects are never followed: Access
  answers a rejected token with a redirect to its sign-in page (or 401/403), all reported as
  "service token not accepted". Timeout 20 s.
- **What goes up.** Unacked runs and unacked inbox events, oldest first, at most 100 of each per
  request, looping over the backlog until it is sent or a request fails. The queue is read again
  before every request, so events queued during the sync (a renumbered item's new creation) go
  in the same sync, and events the ledger dropped meanwhile do not. Events are the ledger's
  public payloads (Mac-only fields are stripped again here); runs are projected onto the
  contract's run fields. Nothing derived from transcript text or local titles is sent.
- **Two items, one id.** A "conflict" ack (the server holds that id for another item: a reset
  ledger, or a second GHOSTED_HOME on the same Worker) renumbers the local item at once
  (``ledger.merge_server_items(conflicts=...)``), before the next request can carry its old id;
  ``SyncResult.renumbered`` lists them. ``pull()`` (one request, no events) gives a new or reset
  ledger the server's ids before it assigns its first one (``core.runner.ensure_server_view``).
- **Lost on the server.** Items this Mac has synced that the server's list no longer has (a D1
  restore, a recreated database) land in ``SyncResult.missing``; ``sync(resync=True)`` after
  ``ledger.reemit_events(ids, force=True)`` sends them again (`ghosted resync`).
- **What comes back.** Acks are recorded (``ledger.record_acks``; stale and invalid ones too, so
  they are never resent), run acks likewise, then the server's items are merged
  (``ledger.merge_server_items``: the server wins status fields) and its notification rows are
  mirrored into ``notifications.jsonl`` (rows the ledger lacks, matched by server id or
  ``dedup_key``; rows still being delivered, ``ok`` null, wait for a later sync). An item with a
  local status change the server has not acknowledged yet keeps its local status until it has.
- **Failure.** A network error, an auth failure or a bad response gives
  ``SyncResult(ok=False, error=<short text>)``; whatever was not acknowledged stays queued for the
  next run. Request and response bodies are never logged or put into errors.

Dev mode, for the local end-to-end test only: env ``GHOSTED_DEV_INSECURE == "1"`` AND a base host of
``localhost`` / ``127.0.0.1`` sends ``x-dev-identity: sync`` instead of the service-token headers.
"""
from __future__ import annotations

import copy
import json
import os
import urllib.parse
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Set, Tuple

from core import __version__, sanitize, timeutil
from core.ledger import ACK_RESULTS, MAC_ONLY_FIELDS, Ledger, LedgerBusy, LedgerError
from notify import base
from notify.base import SendResult, SyncResult

NAME = "remote"
TIMEOUT = 20.0
PULL_TIMEOUT = 5.0  # `ghosted add` on a never-synced ledger waits at most this long for the server
MAX_REQUESTS = 1000  # per sync; a backlog of 100 000 events
MAX_RENUMBER_ROUNDS = 3  # requests per sync that may renumber; a server refusing every id stops here
PURGE_DAYS = 90  # the Worker purges closed items after this (contract 15)
CREATION_TYPES = ("item.add", "item.upsert")
MAX_EVENTS = 100  # per request (contract 14)
MAX_RUNS = 100  # per request, so a long offline stretch can't make one huge request
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
SYNC_PATH = "/api/sync"
NOTIFICATIONS_SLACK = timedelta(days=1)  # re-ask a day back; mirroring dedups, so overlap is free
DEV_FLAG = "GHOSTED_DEV_INSECURE"
DEV_HOSTS = ("localhost", "127.0.0.1")
TEST_PUSH_TITLE_MAX = 100
TEST_PUSH_BODY_MAX_BYTES = 1024
DEFAULT_TEST_BODY = "Test push from Ghosted"
PAIR_TTL_MINUTES = 10

EVENT_KEYS = ("event_id", "type", "at", "item")
RUN_ERROR_MAX = 100
RUN_ERROR_GENERIC = "run failed (details in the Mac's run log)"


def _instant(now: Any) -> datetime:
    return timeutil.now_utc() if now is None else timeutil.parse_iso(now)


def _int_or_none(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def public_event(event: Mapping[str, Any]) -> Dict[str, Any]:
    """An inbox event as uploaded: the four contract keys, Mac-only item fields stripped (the
    ledger never writes them; this is the second gate)."""
    out = {k: copy.deepcopy(event[k]) for k in EVENT_KEYS if k in event}
    item = out.get("item")
    if isinstance(item, dict):
        for key in MAC_ONLY_FIELDS:
            item.pop(key, None)
    return out


def _run_error(value: Any) -> Optional[str]:
    if value is None or value is False:
        return None
    text = " ".join(str(value).split())[:RUN_ERROR_MAX]
    if not text:
        return None
    return RUN_ERROR_GENERIC if sanitize.problems(text) else text


def public_run(run: Mapping[str, Any]) -> Dict[str, Any]:
    """A runs.jsonl record projected onto the contract's run fields (nothing else is uploaded)."""
    ok = run.get("ok")

    def count(key: str) -> int:
        value = _int_or_none(run.get(key))
        return value if value is not None and value >= 0 else 0

    return {
        "run_id": run.get("run_id"),
        "started_at": _str_or_none(run.get("started_at")),
        "finished_at": _str_or_none(run.get("finished_at")),
        "ok": ok is True or (type(ok) in (int, float) and ok == 1),
        "sessions_scanned": count("sessions_scanned"),
        "items_added": count("items_added"),
        "error": _run_error(run.get("error")),
    }


def _test_push_param(test_push: Any) -> Optional[Dict[str, str]]:
    if test_push is None or test_push is False:
        return None
    if test_push is True:
        test_push = {}
    if not isinstance(test_push, Mapping):
        raise TypeError("test_push must be None or {'title': ..., 'body': ...}")
    title = base.cap_chars(base.push_title(test_push.get("title")), TEST_PUSH_TITLE_MAX)
    body = test_push.get("body")
    if not isinstance(body, str) or not body.strip():
        body = DEFAULT_TEST_BODY
    return {"title": title, "body": base.cap_utf8(body, TEST_PUSH_BODY_MAX_BYTES)}


def _pair_param(pair: Any) -> Optional[Dict[str, int]]:
    if pair is None or pair is False:
        return None
    if pair is True:
        return {"ttl_minutes": PAIR_TTL_MINUTES}
    ttl = _int_or_none(pair)
    if ttl is None and isinstance(pair, Mapping):
        ttl = _int_or_none(pair.get("ttl_minutes", PAIR_TTL_MINUTES))
    if ttl is None or ttl < 1:
        raise ValueError("pair must be None, True, a number of minutes or {'ttl_minutes': N}")
    return {"ttl_minutes": ttl}


def _test_push_result(raw: Any) -> Optional[Dict[str, int]]:
    if not isinstance(raw, dict):
        return None
    sent, failed = _int_or_none(raw.get("sent")), _int_or_none(raw.get("failed"))
    return {"sent": sent or 0, "failed": failed or 0}


def _pair_result(raw: Any) -> Optional[Dict[str, Optional[str]]]:
    if not isinstance(raw, dict) or not _str_or_none(raw.get("code")):
        return None
    return {"code": raw["code"], "expires_at": _str_or_none(raw.get("expires_at"))}


def _shape_problem(response: Mapping[str, Any]) -> Optional[str]:
    for key, kind in (("acks", dict), ("runs_acked", list), ("items", list), ("notifications", list)):
        value = response.get(key)
        if value is not None and not isinstance(value, kind):
            return "unexpected response from the server (%s is not a%s)" % (
                key, "n object" if kind is dict else " list")
    return None


class RemoteAdapter(base.Adapter):
    name = NAME
    owns_schedule = True

    def __init__(self, cfg: Any, env: Optional[Mapping[str, str]] = None, *,
                 ledger: Optional[Ledger] = None, timeout: float = TIMEOUT,
                 transport: Optional[Callable[..., base.HttpResponse]] = None,
                 environ: Optional[Mapping[str, str]] = None) -> None:
        self.cfg = cfg
        self.env = dict(env or {})
        self.ledger = ledger
        url = getattr(cfg, "remote_url", "") or ""
        self.base_url = url.strip() if isinstance(url, str) else ""
        self.client_id = self.env.get("GHOSTED_SERVICE_TOKEN_ID") or None
        self.client_secret = self.env.get("GHOSTED_SERVICE_TOKEN_SECRET") or None
        self.timeout = float(timeout)
        self._transport = transport
        self._environ = os.environ if environ is None else environ

    # -- target and auth ---------------------------------------------------------------------------

    def _dev_mode(self, host: str) -> bool:
        flag = self.env.get(DEV_FLAG)
        if flag is None:
            flag = self._environ.get(DEV_FLAG)
        return flag == "1" and host in DEV_HOSTS

    def _target(self) -> Tuple[Optional[str], Dict[str, str], str, Optional[str]]:
        """(sync url, headers, display origin, problem). Headers carry the credentials."""
        parts, problem = base.parse_url(self.base_url, "notify.remote.url")
        if problem:
            return None, {}, "", problem
        assert parts is not None
        where = base.origin(parts)
        path = parts.path.rstrip("/")
        if not path.endswith(SYNC_PATH):
            path += SYNC_PATH
        url = urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._dev_mode((parts.hostname or "").lower()):
            headers["x-dev-identity"] = "sync"
            return url, headers, where, None
        missing = [key for key, value in (("GHOSTED_SERVICE_TOKEN_ID", self.client_id),
                                          ("GHOSTED_SERVICE_TOKEN_SECRET", self.client_secret)) if not value]
        if missing:
            return None, {}, where, "%s missing from .env" % " and ".join(missing)
        headers["CF-Access-Client-Id"] = self.client_id or ""
        headers["CF-Access-Client-Secret"] = self.client_secret or ""
        return url, headers, where, None

    def _client(self) -> Dict[str, Any]:
        return {"version": __version__, "tz": getattr(self.cfg, "timezone", None) or "UTC"}

    def _post(self, url: str, headers: Mapping[str, str], where: str,
              payload: Mapping[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[int]]:
        """(response object, None, status) or (None, short error, status or None)."""
        data = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        transport = self._transport or base.http_request
        try:
            resp = transport(url, method="POST", body=data, headers=dict(headers),
                             timeout=self.timeout, max_bytes=MAX_RESPONSE_BYTES)
        except base.TransportError as exc:
            return None, "cannot reach %s: %s" % (where, exc), None
        status = resp.status
        who = "dev identity" if "x-dev-identity" in headers else "service token"
        if 300 <= status < 400:
            return None, ("%s not accepted (HTTP %d: Access redirected to its sign-in page)"
                          % (who, status)), status
        code = self._error_code(resp)
        if status in (401, 403):
            return None, "%s not accepted (HTTP %d%s)" % (who, status, code), status
        if status >= 500:
            return None, "server error at %s (HTTP %d%s)" % (where, status, code), status
        if status != 200:
            return None, "%s rejected the sync (HTTP %d%s)" % (where, status, code), status
        obj = resp.json()
        if obj is None:
            return None, ("unexpected response from %s (not JSON: a sign-in page or a captive "
                          "portal?)" % where), status
        return obj, None, status

    def _error_code(self, resp: base.HttpResponse) -> str:
        obj = resp.json() or {}
        code = base.safe_text(obj.get("error"), 40, redact=(self.client_id, self.client_secret))
        return (": " + code) if code else ""

    # -- sync --------------------------------------------------------------------------------------

    def pull(self, ledger: Optional[Ledger] = None, now: Any = None, *,
             timeout: Optional[float] = None) -> SyncResult:
        """One request with no events and no runs: the server's items and max_seq, merged, so
        the ids this ledger assigns next start after every id the server holds. Used before a
        new or reset ledger assigns its first id (core.runner.ensure_server_view)."""
        led = ledger or self.ledger or Ledger(self.cfg)
        when = _instant(now)
        result = SyncResult(ok=False)
        url, headers, where, problem = self._target()
        if problem or url is None:
            result.error = problem
            return result
        payload = {"client": self._client(), "runs": [], "events": [],
                   "want": {"items": True, "notifications_since": timeutil.to_iso(when), "test_push": None}}
        saved = self.timeout
        if timeout is not None:
            self.timeout = float(timeout)
        try:
            response, error, status = self._post(url, headers, where, payload)
        finally:
            self.timeout = saved
        result.requests, result.status = 1, status
        if error is None and response is not None:
            error = _shape_problem(response)
        if error is not None or response is None:
            result.error = error or "no response"
            return result
        try:
            self._apply(led, response, when, result, {})
        except (LedgerBusy, LedgerError, OSError) as exc:
            result.error = "could not update the local ledger: %s" % (
                base.safe_text(str(exc)) if isinstance(exc, (LedgerBusy, LedgerError))
                else base.safe_text(getattr(exc, "strerror", None) or "") or type(exc).__name__)
            return result
        result.ok = True
        return result

    def sync(self, ledger: Optional[Ledger] = None, now: Any = None, *, test_push: Any = None,
             pair: Any = None, revoke_devices: bool = False, resync: bool = False) -> SyncResult:
        """Send unacked runs and events, record acks, merge items, mirror notifications.

        ``test_push={"title","body"}`` asks the Worker to push to every subscription now (the
        answer lands in ``SyncResult.test_push``); ``pair={"ttl_minutes": 10}`` (or True) asks for
        a pairing code (``SyncResult.pair``, None when the Worker gave none, e.g. outside
        AUTH_MODE device); ``revoke_devices=True`` empties the Worker's device tokens. These ride
        on the first request only. ``resync=True`` (`ghosted resync`) asks the Worker to store items
        again whose ids it has seen before (after a restore it lost them)."""
        led = ledger or self.ledger or Ledger(self.cfg)
        when = _instant(now)
        want_test = _test_push_param(test_push)
        want_pair = _pair_param(pair)
        result = SyncResult(ok=False)
        url, headers, where, problem = self._target()
        if problem or url is None:
            result.error = problem
            return result
        try:
            error = self._exchange(led, when, url, headers, where, want_test, want_pair,
                                   bool(revoke_devices), result, bool(resync))
        except (LedgerBusy, LedgerError, OSError) as exc:
            # The local files, not the server: whatever was acknowledged before this point is
            # recorded; the rest is sent again (and answered as duplicates) next time.
            error = "could not update the local ledger: %s" % (
                base.safe_text(str(exc)) if isinstance(exc, (LedgerBusy, LedgerError))
                else base.safe_text(getattr(exc, "strerror", None) or "") or type(exc).__name__)
        result.error = error
        result.ok = error is None
        return result

    def _exchange(self, led: Ledger, when: datetime, url: str, headers: Mapping[str, str], where: str,
                  want_test: Optional[Dict[str, str]], want_pair: Optional[Dict[str, int]],
                  revoke_devices: bool, result: SyncResult, resync: bool = False) -> Optional[str]:
        """The request loop of ``sync``; fills ``result`` and returns the error text or None."""
        since = self._notifications_since(led)
        sent: Dict[str, Dict[str, Any]] = {}  # event_id -> event, in send order
        sent_runs: Set[str] = set()
        unanswered: List[str] = []
        last: Optional[Dict[str, Any]] = None
        error: Optional[str] = None
        renumber_rounds = 0
        for index in range(MAX_REQUESTS):
            # Read the queue again each time: a renumbered item's new creation goes in this
            # sync, and events dropped meanwhile (its old id) never go at all.
            chunk = [ev for ev in led.unacked_events() if ev["event_id"] not in sent][:MAX_EVENTS]
            run_chunk: List[Dict[str, Any]] = []
            for run in led.unacked_runs():
                run_id = run.get("run_id")
                if (isinstance(run_id, str) and run_id and run_id not in sent_runs
                        and all(r.get("run_id") != run_id for r in run_chunk)):
                    run_chunk.append(run)
                    if len(run_chunk) >= MAX_RUNS:
                        break
            if index and not chunk and not run_chunk:
                break
            want: Dict[str, Any] = {"items": True, "notifications_since": since,
                                    "test_push": want_test if index == 0 else None}
            if index == 0 and want_pair is not None:
                want["pair"] = want_pair
            if index == 0 and revoke_devices:
                want["revoke_devices"] = True
            if resync:
                want["resync"] = True
            payload = {"client": self._client(), "runs": [public_run(r) for r in run_chunk],
                       "events": [public_event(ev) for ev in chunk], "want": want}
            for ev in chunk:
                sent[ev["event_id"]] = ev
            sent_runs.update(r["run_id"] for r in run_chunk)
            response, error, status = self._post(url, headers, where, payload)
            result.requests += 1
            result.status = status
            if error is None and response is not None:
                error = _shape_problem(response)
            if error is not None or response is None:
                break
            ids = {ev["event_id"] for ev in chunk}
            acks = self._acks(response.get("acks"), {eid: sent[eid] for eid in ids})
            if acks:
                led.record_acks(acks, when)
                result.acks.update(acks)
            refused = [ack["item_id"] for eid, ack in acks.items()
                       if ack["result"] == "conflict" and sent[eid].get("type") in CREATION_TYPES
                       and isinstance(ack.get("item_id"), str)]
            if refused:
                renumber_rounds += 1
                summary = led.merge_server_items([], _int_or_none(response.get("max_seq")), when,
                                                 conflicts=refused)
                result.renumbered.extend(summary.get("renumbered", []))
                if renumber_rounds >= MAX_RENUMBER_ROUNDS:
                    # Ids past max_seq should never be refused; a server that keeps refusing is
                    # not one to keep feeding. The renumbered items go next sync.
                    last = response
                    error = "the server refused %d item id%s in %d requests; ghosted sync tries again" % (
                        len(result.renumbered), "" if len(result.renumbered) == 1 else "s", renumber_rounds)
                    break
            unanswered.extend(ev["event_id"] for ev in chunk if ev["event_id"] not in acks)
            run_ids = {r["run_id"] for r in run_chunk}
            acked_runs = []
            for run_id in response.get("runs_acked") or []:
                if isinstance(run_id, str) and run_id in run_ids and run_id not in acked_runs:
                    acked_runs.append(run_id)
            if acked_runs:
                led.record_runs_acked(acked_runs)
                result.runs_acked.extend(acked_runs)
            if index == 0:
                result.test_push = _test_push_result(response.get("test_push"))
                result.pair = _pair_result(response.get("pair"))
                for key in ("revoked", "devices_revoked"):
                    if _int_or_none(response.get(key)) is not None:
                        result.revoked = response[key]
                        break
            last = response

        if last is not None:
            self._apply(led, last, when, result, sent)
        if error is None and unanswered:
            error = ("the server did not acknowledge %d event%s; %s sent again next time"
                     % (len(unanswered), "" if len(unanswered) == 1 else "s",
                        "it is" if len(unanswered) == 1 else "they are"))
        return error

    @staticmethod
    def _acks(raw: Any, sent: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """Valid acks for events of this request only. An ack with an unknown result is left
        out, so its event is sent again."""
        out: Dict[str, Dict[str, Any]] = {}
        if not isinstance(raw, dict):
            return out
        for event_id, ack in raw.items():
            event = sent.get(event_id)
            if event is None or not isinstance(ack, dict) or ack.get("result") not in ACK_RESULTS:
                continue
            item_id = _str_or_none(ack.get("item_id"))
            if item_id is None:
                item_id = _str_or_none((event.get("item") or {}).get("id"))
            out[event_id] = {"result": ack["result"], "item_id": item_id,
                             "rev": _int_or_none(ack.get("rev")),
                             "error": base.safe_text(ack.get("error")) or None}
        return out

    def _apply(self, led: Ledger, response: Mapping[str, Any], when: datetime, result: SyncResult,
               sent: Mapping[str, Mapping[str, Any]]) -> None:
        """Merge the last good response: items, next_id, stale/invalid lists, notifications."""
        result.server_time = _str_or_none(response.get("server_time"))
        items = response.get("items")
        rows = [it for it in items if isinstance(it, dict)] if isinstance(items, list) else []
        current = {it["id"]: it.get("status") for it in rows if isinstance(it.get("id"), str)}
        max_seq = _int_or_none(response.get("max_seq"))
        if rows or max_seq is not None:
            # A status change this Mac made that the server has not acknowledged yet keeps its
            # local value until the server answers it (applied or stale); then the server wins.
            pending = set()
            for ev in led.unacked_events():
                item = ev.get("item")
                if ev.get("type") == "item.status" and isinstance(item, dict) and isinstance(item.get("id"), str):
                    pending.add(item["id"])
            keep = [it for it in rows if not (isinstance(it.get("id"), str) and it["id"] in pending)]
            summary = led.merge_server_items(keep, max_seq, when)
            result.merged = len(summary.get("added", [])) + len(summary.get("changed", []))
            result.renumbered.extend(summary.get("renumbered", []))
            if isinstance(items, list):
                result.missing = _missing_on_server(led, current, pending, when)
        push = response.get("push")
        result.push = dict(push) if isinstance(push, dict) else None
        for event_id in sent:
            ack = result.acks.get(event_id)
            if ack is None:
                continue
            if ack["result"] == "stale":
                # One entry per item: a re-snooze (reopen + snooze) can bring two stale acks.
                entry = (ack["item_id"], current.get(ack["item_id"]))
                if all(seen[0] != entry[0] for seen in result.stale):
                    result.stale.append(entry)
            elif ack["result"] == "invalid":
                result.invalid.append((event_id, ack["item_id"], ack.get("error")))
        notes = response.get("notifications")
        if isinstance(notes, list):
            result.mirrored = self._mirror(led, notes, when)

    # -- notifications mirror ----------------------------------------------------------------------

    @staticmethod
    def _notifications_since(led: Ledger) -> Optional[str]:
        latest: Optional[datetime] = None
        for row in led.notifications():
            if row.get("source") != NAME:
                continue
            try:
                sent_at = timeutil.parse_iso(row.get("sent_at"))
            except (TypeError, ValueError):
                continue
            if latest is None or sent_at > latest:
                latest = sent_at
        return None if latest is None else timeutil.to_iso(latest - NOTIFICATIONS_SLACK)

    @staticmethod
    def _mirror(led: Ledger, rows: List[Any], when: datetime) -> int:
        stamp = timeutil.to_iso(when)
        added = 0
        with led.lock():
            existing = led.notifications()
            keys = {r.get("dedup_key") for r in existing if _str_or_none(r.get("dedup_key"))}
            ids = {r.get("id") for r in existing if r.get("source") == NAME
                   and isinstance(r.get("id"), (int, str)) and not isinstance(r.get("id"), bool)}
            for row in rows:
                if not isinstance(row, dict) or row.get("ok") is None:
                    continue  # still being delivered: a later sync mirrors its final state
                key = _str_or_none(row.get("dedup_key"))
                srv_id = row.get("id")
                if isinstance(srv_id, bool) or not isinstance(srv_id, (int, str)):
                    srv_id = None
                if key is None and srv_id is None:
                    continue
                if (key is not None and key in keys) or (srv_id is not None and srv_id in ids):
                    continue
                led.append_notification(_mirror_row(row, key, srv_id, stamp))
                if key is not None:
                    keys.add(key)
                if srv_id is not None:
                    ids.add(srv_id)
                added += 1
        return added

    # -- send and doctor ---------------------------------------------------------------------------

    def send(self, items: Any, *, title: Optional[str], body: Optional[str], kind: str,
             priority: str = "normal", click_url: Optional[str] = None) -> SendResult:
        """A test push through the Worker: one sync with want.test_push. The Worker schedules
        the real pushes itself (owns_schedule), so ``kind``/``priority``/``click_url`` are not
        sent."""
        rows = base.iter_items(items)
        head = base.push_title(title)
        text = base.push_body(rows, body, "") or DEFAULT_TEST_BODY
        leak = base.local_title_leak(rows, head, text, click_url)
        if leak:
            return SendResult(False, NAME, leak)
        res = self.sync(self.ledger, None, test_push={"title": head, "body": text})
        pushed = res.test_push
        if pushed is None:
            return SendResult(False, NAME, res.error or "the server did not report the test push",
                              res.status)
        sent, failed = pushed["sent"], pushed["failed"]
        if sent >= 1:
            note = "delivered to %d of %d subscriptions" % (sent, sent + failed) if failed else res.error
            return SendResult(True, NAME, note, res.status)
        if failed:
            return SendResult(False, NAME, "the push failed for all %d subscription%s"
                              % (failed, "" if failed == 1 else "s"), res.status)
        return SendResult(False, NAME, "no phone has notifications on yet (open the app and tap "
                                       "Enable notifications)", res.status)

    def check(self) -> Tuple[bool, str]:
        """One empty sync (no runs, no events, no items): proves the URL and the credentials, and
        that the Worker reads due and snooze times in this Mac's timezone."""
        url, headers, where, problem = self._target()
        if problem or url is None:
            return False, "remote: " + (problem or "not configured")
        now = timeutil.now_utc()
        payload = {"client": self._client(), "runs": [], "events": [],
                   "want": {"items": False, "notifications_since": timeutil.to_iso(now),
                            "test_push": None}}
        response, error, _status = self._post(url, headers, where, payload)
        if error is not None or response is None:
            return False, "remote: " + (error or "no response")
        who = "dev identity" if "x-dev-identity" in headers else "service token"
        mine = self._client()["tz"]
        theirs = response.get("tz")
        if isinstance(theirs, str) and theirs and not same_zone(theirs, mine, now):
            # Due and snooze values are local wall times: a Worker in another zone pushes them at
            # the wrong hour (and runs quiet hours and the digest on its own clock).
            return False, ("remote %s reachable; %s accepted; but the Worker runs in %s and this Mac in %s: "
                           "set TZ_NAME in wrangler.jsonc (or timezone in config.toml) so they match"
                           % (where, who, base.safe_text(theirs, 64), mine))
        return True, "remote %s reachable; %s accepted" % (where, who)


def _missing_on_server(led: Ledger, on_server: Mapping[str, Any], pending: Set[str], when: datetime) -> List[str]:
    """Ids this Mac holds as synced that the server's full list lacks, in id order: the server
    lost them (a D1 restore, a recreated database). Closed items older than the Worker's purge
    and items with an unsent change are left out."""
    horizon = when - timedelta(days=PURGE_DAYS)
    out = []
    for item_id, item in led.items().items():
        if not item.get("synced") or item_id in on_server or item_id in pending:
            continue
        if item.get("status") in ("done", "dismissed"):
            try:
                closed = timeutil.parse_iso(item.get("closed_at"))
            except (TypeError, ValueError):
                closed = None
            if closed is None or closed < horizon:
                continue
        out.append(item_id)
    return out


def same_zone(a: str, b: str, now: datetime) -> bool:
    """True for the same IANA name, or for two names that give the same UTC offset all through the
    coming year (aliases such as America/Chicago and US/Central)."""
    if a == b:
        return True
    try:
        za, zb = timeutil.get_tz(a), timeutil.get_tz(b)
    except ValueError:
        return False
    start = now.replace(minute=0, second=0, microsecond=0)
    for hours in range(0, 366 * 24, 6):
        at = start + timedelta(hours=hours)
        if at.astimezone(za).utcoffset() != at.astimezone(zb).utcoffset():
            return False
    return True


def _mirror_row(row: Mapping[str, Any], key: Optional[str], srv_id: Any, stamp: str) -> Dict[str, Any]:
    """A server notification row as stored locally (the shape core.schedule and ghosted report read)."""
    item_ids = row.get("item_ids")
    if isinstance(item_ids, str):  # D1 keeps item_ids as JSON text; accept it either way
        try:
            item_ids = json.loads(item_ids)
        except ValueError:
            item_ids = []
    ids = [i for i in item_ids if isinstance(i, str)] if isinstance(item_ids, list) else []
    ok = row.get("ok")
    return {
        "id": srv_id,
        "dedup_key": key,
        "kind": _str_or_none(row.get("kind")),
        "item_ids": ids,
        "sent_at": _str_or_none(row.get("sent_at")),
        "local_date": _str_or_none(row.get("local_date")),
        "ok": 1 if (ok is True or (type(ok) in (int, float) and ok == 1)) else 0,
        "channel": base.safe_text(row.get("channel"), 40) or None,
        "error": base.safe_text(row.get("error")) or None,
        "source": NAME,
        "mirrored_at": stamp,
    }
