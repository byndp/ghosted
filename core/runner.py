"""``ghosted run`` (the hourly job) and ``ghosted sweep``: one scan, the digest, then pushes or the remote sync.

``run(cfg, now=None, notify=True, scan=True) -> RunOutcome``

1. Take ``run.lock`` without waiting. Busy (another scan is running): record nothing and return
   ``RunOutcome(skipped=True)``.
2. Scan (``core.detector.scan``). A scan that raises makes the run ``ok = False`` with
   ``error = "<ExceptionType>: <short message>"`` (messages only from this repo's own exception
   types and OS errors; anything else is the type name alone, so transcript text can never ride
   along). Per-file failures inside a scan do not fail the run; they are in ``RunOutcome.scan``.
3. Rewrite ``digest.md``, then append the run record to ``runs.jsonl`` (before any sync, so the
   sync carries this run as the heartbeat).
4. Notify (``notify=True``): ``adapter = notify.get_adapter(cfg, core.config.load_env(cfg))``.
   - An adapter that owns the schedule (remote): ``adapter.sync(ledger, now)``, then the digest is
     rendered again with the sync status and the statuses the phone changed. The Worker reopens
     due snoozes itself.
   - A local adapter (ntfy, Pushover): ``core.schedule.decide(...)`` with the dead-man off; due
     snoozes reopen (``ledger.reopen_expired``: rev + 1, snooze_until kept); each push first
     writes its claim row (``ledger.claim_dedup``: ``ok`` null, fsync'd), is sent with public
     item fields only, then its outcome row is appended, whatever the outcome (a failed send is
     not retried: the row dedups it; a crash before the outcome row leaves the claim, which
     dedups too). Before any claim the adapter's ``reachable()`` must open a connection to its
     server (retrying for up to ~30 s): a Mac woken offline (DNS failing, connection refused)
     claims nothing, so the next tick in the window sends the push instead of the day losing it.
     An interrupted item that a newer flagged ending reopened counts for T4 from its
     ``reopened_at`` (``t4_items``).
   - Adapter ``none`` (pushes off): due snoozes still reopen, so a snoozed item comes back to
     the digest and ``ghosted list`` at its time.

5. Watchdog (contract 16.5, spec 8): after an ok run under ``GHOSTED_SCHEDULED=1`` with
   ``[watchdog] ping_url`` set, one best-effort GET (``watchdog_ping``); its outcome is in
   ``RunOutcome.watchdog`` and never fails the run.

``sweep(cfg, now=None, scan=True) -> str``: the same scan without notifications or a run record;
writes and returns the digest. When another scan holds ``run.lock`` it returns the existing digest
(or a fresh render without a scan) with a note at the top.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import time
import uuid as uuid_mod
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional
from urllib.parse import urlsplit

from core import classify
from core import config as config_mod
from core import detector, digest, evidence, sanitize, schedule, timeutil
from core import ledger as ledger_mod

_AUTO = object()  # run(adapter=...) default: ask notify.get_adapter

BUSY_NOTE = "Another scan is running; this is the last digest it wrote."
BUSY_NOTE_FRESH = "Another scan is running; this digest was rendered without scanning."


@dataclass
class RunOutcome:
    """What one ``run()`` did.

    ``run``: the record appended to runs.jsonl (None when skipped). ``scan``: the ScanResult (None
    when skipped, not scanning, or the scan raised). ``notify_result``: None without notifications;
    the adapter's SyncResult for the remote adapter (or ``{"ok": False, "error": ...}`` when the
    sync raised); for a local adapter ``{"reopened": [ids], "sent": [outcome rows],
    "error": None|str}``; for adapter none the same dict when a snooze expired (or failed to),
    else None. ``skipped``: another scan held run.lock. ``digest``: the digest text.
    ``watchdog``: None when no ping was due, else ``{"ok", "where", "status", "error"}`` (``where``
    is the URL's origin only: the full ping URL is a credential and is never recorded).
    """

    run: Optional[Dict[str, Any]] = None
    scan: Optional[detector.ScanResult] = None
    notify_result: Any = None
    skipped: bool = False
    digest: Optional[str] = None
    watchdog: Optional[Dict[str, Any]] = None


# ---- run.lock ------------------------------------------------------------------------------------

@contextlib.contextmanager
def run_lock(cfg: Any) -> Iterator[bool]:
    """``flock(LOCK_EX | LOCK_NB)`` on GHOSTED_HOME/run.lock: yields True when held, False when busy."""
    home = Path(cfg.home)
    ledger_mod.ensure_dir(home)
    fd = os.open(str(home / "run.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                yield False
                return
            raise
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---- helpers -------------------------------------------------------------------------------------

# Exception types whose messages are written by this repo (or the OS) and never quote a transcript.
_SAFE_MESSAGE_TYPES = (ledger_mod.LedgerError, ledger_mod.LedgerBusy, ledger_mod.UnknownItem,
                       config_mod.ConfigError, sanitize.Refused)


def describe(exc: BaseException) -> str:
    """``"<Type>: <short message>"`` for a run record or a note. OSError keeps only its strerror
    (its filename could name a client's folder); unknown exception types keep only their name."""
    name = type(exc).__name__
    if isinstance(exc, OSError):
        message = exc.strerror or ""
    elif isinstance(exc, _SAFE_MESSAGE_TYPES):
        message = str(exc)
    else:
        message = ""
    message = " ".join(message.split())[:120]
    return "%s: %s" % (name, message) if message else name


def _now(now: Any) -> datetime:
    return timeutil.now_utc() if now is None else timeutil.parse_iso(now)


def _last_run(led: Any) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_at: Optional[datetime] = None
    for row in led.runs():
        try:
            at = timeutil.parse_iso(row.get("finished_at") or row.get("started_at"))
        except (ValueError, TypeError):
            continue
        if best_at is None or at >= best_at:
            best, best_at = row, at
    return best


def default_sync_label(cfg: Any) -> str:
    adapter = getattr(cfg, "adapter", "none")
    if adapter == "remote":
        return "sync on next run"
    if adapter in ("ntfy", "pushover", "macos"):
        return "push via " + adapter
    return "local only"


def _scan_notes(result: Optional[detector.ScanResult]) -> List[str]:
    if result is None or not result.errors:
        return []
    kinds = sorted(set(result.errors))
    return ["%d session file(s) could not be scanned (%s)" % (len(result.errors), ", ".join(kinds))]


def _write_digest(cfg: Any, led: Any, now: datetime, run: Optional[Mapping[str, Any]], sync: Any,
                  notes: Optional[List[str]] = None) -> str:
    live = digest.live_waiting(led.stops(), led.load_state(), now, cfg)
    text = digest.render(led.items(), now, cfg.timezone, run=run, sync=sync, live=live, notes=notes)
    led.write_digest(text)
    return text


def _get_adapter(cfg: Any) -> Any:
    """``notify.get_adapter(cfg, load_env(cfg))``; None for adapter "none" (notify is not imported)."""
    if getattr(cfg, "adapter", "none") in (None, "", "none"):
        return None
    import notify  # the package is optional for a core-only install

    return notify.get_adapter(cfg, config_mod.load_env(cfg))


def _push_cfg(cfg: Any) -> Dict[str, Any]:
    return {"digest_hour": cfg.digest_hour, "quiet_start": cfg.quiet_start, "quiet_end": cfg.quiet_end,
            "daily_budget": cfg.daily_budget, "deadman_hours": schedule.DEFAULTS["deadman_hours"],
            "enable_deadman": False}


def _instant_or_none(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return timeutil.parse_iso(value)
    except ValueError:
        return None


def t4_items(items: Any) -> List[Dict[str, Any]]:
    """The items as the push rules should see them: an interrupted item that a newer flagged
    ending reopened or un-resolved (``reopened_at``, Mac only) is a new interruption from then,
    so its T4 time (``created_at`` in decide) is the later of the two. Copies; never mutates."""
    out: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping) and item.get("kind") == "interrupted":
            again = _instant_or_none(item.get("reopened_at"))
            created = _instant_or_none(item.get("created_at"))
            if again is not None and (created is None or again > created):
                item = dict(item, created_at=item["reopened_at"])
        out.append(item)  # type: ignore[arg-type]
    return out


def interrupted_tier(cfg: Any, item: Mapping[str, Any]) -> Optional[str]:
    """The tier (contract 16.1) of an interrupted item, from what the item carries: the verdict
    from its fixed title (usage limit vs open question), its ``reason``, and for a save offer
    whether its evidence says the work is still unsaved. None for any other kind."""
    if item.get("kind") != "interrupted":
        return None
    verdict = classify.USAGE_LIMIT if detector.ending_of(item) == evidence.ENDING_LIMIT else classify.OPEN_QUESTION
    return classify.tier(verdict, item.get("reason"), getattr(cfg, "direct_questions", classify.WAITING),
                         unsaved=evidence.unsaved_from_text(item.get("evidence")))


def push_candidates(cfg: Any, items: Any) -> List[Dict[str, Any]]:
    """The items local pushes may carry (contract 16.5): every item except an interrupted one whose
    tier is soft (a polite offer, a saved save-offer) and an unsaved-work item (contract 16.9: the
    report shows those, pushes never do). Promise and decision items are kept."""
    return [item for item in items
            if not (isinstance(item, Mapping) and (interrupted_tier(cfg, item) == classify.SOFT
                                                   or item.get("kind") == "unsaved"))]


def local_push(cfg: Any, led: Any, adapter: Any, now: datetime) -> Dict[str, Any]:
    """One tick of the push rules for a local adapter (contract 12). With ``adapter`` None
    (pushes off) only the snooze expiry is applied. ``out["unreachable"]`` holds the reason when
    the adapter's server could not be reached, so nothing was claimed this tick.

    Only *waiting* interrupted items reach the send rules (contract 16.5): soft ones are dropped
    before ``schedule.decide``. Snooze expiry still applies to every item, soft ones included."""
    out: Dict[str, Any] = {"reopened": [], "sent": [], "error": None}
    every = t4_items(led.items().values())
    pushable = push_candidates(cfg, every)
    notes, runs = led.notifications(), led.runs()
    decision = schedule.decide(now, pushable, notes, runs, cfg.timezone, _push_cfg(cfg))
    if len(pushable) != len(every):
        # The reopen list comes from every item: a soft item's snooze still ends on time.
        decision["reopen"] = schedule.decide(now, every, notes, runs, cfg.timezone, _push_cfg(cfg)).get("reopen", [])
    if decision.get("reopen"):
        # Snooze expiry keeps snooze_until, so T5 still sees "reopened today" at later ticks.
        out["reopened"] = [item["id"] for item, _ in led.reopen_expired(decision["reopen"], now)]
    if adapter is None or not decision.get("send"):
        return out
    probe = getattr(adapter, "reachable", None)
    if callable(probe):
        # A send attempt is final once claimed. No connection at all proves nothing was
        # delivered, so this tick claims nothing and a later tick in the window tries again.
        ok, why = probe()
        if not ok:
            out["unreachable"] = why or "server unreachable"
            return out
    by_id = led.items()
    channel_default = getattr(adapter, "name", None) or getattr(cfg, "adapter", "?")
    for push in decision["send"]:
        key = push["dedup_key"]
        row = {"dedup_key": key, "local_date": push["local_date"], "sent_at": timeutil.to_iso(now),
               "kind": push["kind"], "item_ids": list(push.get("item_ids", [])), "ok": None,
               "channel": channel_default, "error": None}
        if not led.claim_dedup(key, now, row=row):  # the claim row (ok null) goes in first
            continue
        payload = [ledger_mod.public_fields(by_id[i]) for i in push.get("item_ids", []) if i in by_id]
        try:
            res = adapter.send(payload, title=push["title"], body=push["body"], kind=push["kind"],
                               priority=push.get("priority", "normal"))
            ok = bool(getattr(res, "ok", False))
            channel = getattr(res, "channel", None) or channel_default
            error = getattr(res, "error", None)
        except Exception as exc:  # the outcome is still recorded: a failed send is never retried
            ok, channel, error = False, channel_default, describe(exc)
        result = dict(row, ok=ok, channel=channel,
                      error=None if error is None else " ".join(str(error).split())[:200])
        led.append_notification(result)
        out["sent"].append(result)
    return out


def ensure_server_view(cfg: Any, led: Any, adapter: Any, now: Any = None,
                       timeout: Optional[float] = None) -> bool:
    """For the remote adapter: when this ledger has never learned the server's ids (a new or
    reset ledger), pull the server's items once (no events, no runs) and merge them, so the ids
    it assigns next start after the server's max_seq. True when the ledger has that view after
    the call. Never raises; on any failure ids keep coming from local state, and the server
    answers a reused id with "conflict" (the Mac then renumbers its item)."""
    try:
        if led.has_server_view():
            return True
        pull = getattr(adapter, "pull", None)
        if not callable(pull):
            return False
        result = pull(led, now, timeout=timeout)
        return bool(getattr(result, "ok", False)) and led.has_server_view()
    except Exception:
        return False


# ---- watchdog ------------------------------------------------------------------------------------

SCHEDULED_ENV = "GHOSTED_SCHEDULED"  # set by the launchd job (core.install)
WATCHDOG_TIMEOUT = 10.0
_LOOPBACK = ("localhost", "127.0.0.1")


def _ping_origin(url: str) -> Optional[str]:
    """'https://host[:port]' for a URL the watchdog may GET, else None. https only, except plain
    http to loopback (tests); config.toml itself accepts https only."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme, host = parts.scheme.lower(), (parts.hostname or "").lower()
    if not host or not (scheme == "https" or (scheme == "http" and host in _LOOPBACK)):
        return None
    return "%s://%s%s" % (scheme, host, ":%d" % port if port else "")


def watchdog_ping(cfg: Any, outcome: RunOutcome, environ: Optional[Mapping[str, str]] = None,
                  ping: Any = None) -> Optional[Dict[str, Any]]:
    """Spec 8: after an ok scheduled run (``GHOSTED_SCHEDULED=1``, the launchd job) GET
    ``[watchdog] ping_url`` so a healthchecks-style service notices when the hourly job stops.
    Best effort: one try, 10 s, never raises, never fails the run. A by-hand ``ghosted run``, a
    skipped or failed run, or no URL pings nothing (None). The result names the URL's origin
    only: the path of a ping URL is what lets anyone fake the heartbeat."""
    url = getattr(cfg, "watchdog_ping_url", "") or ""
    env = os.environ if environ is None else environ
    if not isinstance(url, str) or not url.strip() or env.get(SCHEDULED_ENV) != "1":
        return None
    if outcome.skipped or not (outcome.run or {}).get("ok"):
        return None
    url = url.strip()
    where = _ping_origin(url)
    if where is None:
        return {"ok": False, "where": None, "status": None, "error": "ping_url must be an https:// URL"}
    try:
        if ping is None:
            from notify import base  # the package is optional for a core-only install

            ping = base.http_request
        resp = ping(url, method="GET", timeout=WATCHDOG_TIMEOUT, max_bytes=4096)
        status = int(getattr(resp, "status", 0) or 0)
    except Exception as exc:  # noqa: BLE001 - TransportError's text is URL-free; others: the type
        text = str(exc) if type(exc).__name__ == "TransportError" else type(exc).__name__
        return {"ok": False, "where": where, "status": None,
                "error": " ".join(text.split())[:120] or "network error"}
    if 200 <= status < 300:
        return {"ok": True, "where": where, "status": status, "error": None}
    return {"ok": False, "where": where, "status": status, "error": "HTTP %d" % status}


# ---- run and sweep -------------------------------------------------------------------------------

def run(cfg: Any, now: Any = None, notify: bool = True, scan: bool = True, *, adapter: Any = _AUTO,
        environ: Optional[Mapping[str, str]] = None, ping: Any = None) -> RunOutcome:
    """The hourly job (see the module docstring). ``adapter`` overrides ``notify.get_adapter``;
    ``environ`` (default os.environ) and ``ping`` (default ``notify.base.http_request``) are for
    the watchdog ping, which follows an ok scheduled run once run.lock is released."""
    outcome = _run(cfg, now, notify, scan, adapter)
    outcome.watchdog = watchdog_ping(cfg, outcome, environ, ping)
    return outcome


def _run(cfg: Any, now: Any, notify: bool, scan: bool, adapter: Any) -> RunOutcome:
    started_clock = time.monotonic()
    start = _now(now)
    with run_lock(cfg) as held:
        if not held:
            return RunOutcome(skipped=True)
        led = ledger_mod.Ledger(cfg)
        ok, error = True, None
        result: Optional[detector.ScanResult] = None
        if scan and notify and getattr(cfg, "adapter", "none") == "remote":
            # Before the scan assigns ids: a ledger that has never synced learns the server's ids
            # first (a reset ledger, or a second home on the same Worker, would reuse them).
            try:
                ensure_server_view(cfg, led, _get_adapter(cfg) if adapter is _AUTO else adapter, start)
            except Exception:
                pass  # the sync below reports the problem; ids still come from local state
        if scan:
            try:
                result = detector.scan(cfg, led, start)
            except Exception as exc:
                ok, error = False, describe(exc)
        record: Dict[str, Any] = {
            "run_id": str(uuid_mod.uuid4()), "started_at": timeutil.to_iso(start), "finished_at": None,
            "ok": ok, "sessions_scanned": result.sessions_scanned if result else 0,
            "items_added": result.items_added if result else 0, "error": error,
        }
        notes = _scan_notes(result) + ([] if ok else ["scan failed: " + (error or "error")])
        text: Optional[str] = None
        try:
            text = _write_digest(cfg, led, start, record, default_sync_label(cfg) if notify else "notifications off", notes)
        except Exception as exc:
            if record["ok"]:
                record["ok"], record["error"] = False, "digest: " + describe(exc)
        record["finished_at"] = timeutil.to_iso(start + timedelta(seconds=time.monotonic() - started_clock))
        led.append_run(record)

        outcome = RunOutcome(run=record, scan=result, digest=text)
        if not notify:
            return outcome
        try:
            chosen = _get_adapter(cfg) if adapter is _AUTO else adapter
        except Exception as exc:
            outcome.notify_result = {"ok": False, "error": describe(exc)}
            return outcome
        if chosen is None and getattr(cfg, "adapter", "none") == "remote":
            return outcome  # an injected None: the Worker owns snoozes for the remote adapter
        if getattr(chosen, "owns_schedule", False):
            try:
                outcome.notify_result = chosen.sync(led, start)
            except Exception as exc:
                outcome.notify_result = {"ok": False, "error": describe(exc)}
            sync_status: Any = outcome.notify_result
        else:
            try:
                res = local_push(cfg, led, chosen, start)
            except Exception as exc:
                res = {"reopened": [], "sent": [], "error": describe(exc)}
            if chosen is None:
                # pushes are off: report only a snooze expiry (or a failure to apply one)
                if not res.get("reopened") and not res.get("error"):
                    return outcome
                sync_status = default_sync_label(cfg)
            else:
                sync_status = "push via %s" % (getattr(chosen, "name", None) or cfg.adapter)
            outcome.notify_result = res
            if res.get("error"):
                sync_status += (" failed: " if chosen is not None else ", snooze expiry failed: ") + res["error"]
            elif res.get("unreachable"):
                sync_status += " skipped (%s); the next run tries again" % res["unreachable"]
            elif not res.get("reopened") and not res.get("sent"):
                return outcome  # nothing changed: the digest written above stands
        try:
            outcome.digest = _write_digest(cfg, led, start, record, sync_status, notes)
        except Exception:
            pass  # the first digest stands; the run is already recorded
        return outcome


def sweep(cfg: Any, now: Any = None, scan: bool = True) -> str:
    """``ghosted sweep``: scan (no notifications, no run record), rewrite digest.md and return it."""
    start = _now(now)
    led = ledger_mod.Ledger(cfg)
    with run_lock(cfg) as held:
        if not held:
            try:
                existing = led.digest_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                existing = ""
            if existing.strip():
                return "_%s_\n\n%s" % (BUSY_NOTE, existing)
            live = digest.live_waiting(led.stops(), led.load_state(), start, cfg)
            return digest.render(led.items(), start, cfg.timezone, run=_last_run(led),
                                 sync=default_sync_label(cfg), live=live, notes=[BUSY_NOTE_FRESH])
        notes: List[str] = []
        if scan:
            try:
                notes.extend(_scan_notes(detector.scan(cfg, led, start)))
            except Exception as exc:
                notes.append("scan failed: " + describe(exc))
        return _write_digest(cfg, led, start, _last_run(led), default_sync_label(cfg), notes)
