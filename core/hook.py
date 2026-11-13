"""`ghosted hook`: the Stop / StopFailure hook (contract section 11).

Claude Code runs this on every turn end (exec form, async). It reads the hook payload from stdin,
decides a verdict and appends one record to GHOSTED_HOME/stops.jsonl:

    {"at", "session_id", "event", "verdict", "reason", "cwd_project"}

with a single os.write on an O_APPEND descriptor (core.ledger.append_jsonl, no lock), so it never
waits on a scan. No message text is ever written: the record holds machine facts only.

- Stop: the section 7 question test on `last_assistant_message` (open_question with its reason,
  else clean). When the field is missing, the transcript tail is parsed and classified instead.
- StopFailure: error "rate_limit" -> usage_limit (reason rate_limit); anything else -> api_error
  (reason = the error code when it is a plain identifier, else api_error).

The hook only records; the detector creates items once the session goes quiet. `main` never
raises and never prints, whatever the input (empty, not JSON, not an object, missing fields, too
big, an unreadable transcript, an GHOSTED_HOME that can't be written), and returns 0 on every path.
Process-level silence (stdout and stderr on /dev/null, exit 0 even if an import fails) is the
launcher's job: see bin/ghosted.
"""
from __future__ import annotations

import json
import os
import re
import sys
import warnings
from typing import Any, Mapping, Optional, Tuple

MAX_STDIN_BYTES = 1024 * 1024       # contract: the payload is at most 1 MiB
MAX_TAIL_BYTES = 4 * 1024 * 1024    # transcript fallback: the tail window stops growing here
EVENTS = ("Stop", "StopFailure")
STOPS_FILE = "stops.jsonl"

# Claude Code session ids are UUIDs; anything else shaped like an id is accepted, junk is not.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")


def main(stdin_bytes: Any, cfg: Any = None, now: Any = None,
         environ: Optional[Mapping[str, str]] = None) -> int:
    """Record one Stop / StopFailure payload. Never raises, never prints, always returns 0."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. a FutureWarning from a config regex
            _run(stdin_bytes, cfg, now, environ)
    except BaseException:  # noqa: BLE001 - nothing may escape a hook (contract section 11)
        pass
    return 0


def read_stdin(stream: Any = None) -> Optional[bytes]:
    """Up to MAX_STDIN_BYTES + 1 bytes of stdin (one over the limit marks a payload as too big).

    A terminal gives None at once (someone ran `ghosted hook` by hand), as does a closed stdin."""
    try:
        source = sys.stdin if stream is None else stream
        if source is None or source.isatty():
            return None
        raw = getattr(source, "buffer", source)
        data = raw.read(MAX_STDIN_BYTES + 1)
        return data if isinstance(data, bytes) else None
    except BaseException:  # noqa: BLE001
        return None


# ---- the pieces (pure functions, unit-tested) -------------------------------------------------

def parse_payload(data: Any) -> Optional[dict]:
    """The hook payload as a dict, or None for anything that is not one JSON object <= 1 MiB."""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MAX_STDIN_BYTES:
        return None
    try:
        obj = json.loads(bytes(data))
    except (ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def make_record(payload: Mapping[str, Any], cfg: Any, now: Any = None) -> Optional[dict]:
    """The stops.jsonl record for a payload, or None when it is not a usable Stop/StopFailure."""
    event = _event(payload)
    if event is None:
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    if event == "StopFailure":
        verdict, reason = failure_verdict(payload.get("error"))
    else:
        found = stop_verdict(payload, cfg)
        if found is None:
            return None
        verdict, reason = found
    from core import timeutil

    when = timeutil.now_utc() if now is None else timeutil.parse_iso(now)
    return {
        "at": timeutil.to_iso(when),
        "session_id": session_id,
        "event": event,
        "verdict": verdict,
        "reason": reason,
        "cwd_project": cwd_project(payload.get("cwd"), cfg),
    }


def failure_verdict(error: Any) -> Tuple[str, str]:
    """StopFailure: rate_limit -> usage_limit; any other error -> api_error."""
    if error == "rate_limit":
        return "usage_limit", "rate_limit"
    if isinstance(error, str) and _ERROR_CODE_RE.fullmatch(error):
        return "api_error", error
    return "api_error", "api_error"


def question_verdict(text: str, ignore_tail_patterns: Any = ()) -> Tuple[str, str]:
    """The section 7 question test (classifier step 5) on one final text."""
    from core import classify

    cue = classify.question_cue(text)
    if cue is None:
        return classify.CLEAN, classify.NO_QUESTION
    if classify.matches_ignore_tail(text, ignore_tail_patterns):
        return classify.CLEAN, classify.IGNORED_TAIL
    return classify.OPEN_QUESTION, cue


def stop_verdict(payload: Mapping[str, Any], cfg: Any) -> Optional[Tuple[str, str]]:
    """Stop: test last_assistant_message; without it, classify the transcript tail."""
    from core import classify

    patterns = getattr(cfg, "ignore_tail_patterns", classify.DEFAULT_IGNORE_TAIL_PATTERNS)
    message = payload.get("last_assistant_message")
    if isinstance(message, str):
        return question_verdict(message, patterns)
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path:
        return None
    from core import transcript

    summary = transcript.parse_transcript(path, head=False, max_tail_bytes=MAX_TAIL_BYTES)
    ending = classify.classify(summary, patterns, getattr(cfg, "handoff_patterns", None))
    return ending.verdict, ending.reason


def cwd_project(cwd: Any, cfg: Any) -> Optional[str]:
    """The project for a cwd: core.detector.project_for (contract 6.8; stat calls only, no
    subprocess), else the cwd basename (also for a relative cwd, which Claude Code never sends)."""
    if not isinstance(cwd, str) or not cwd.strip() or "\x00" in cwd:
        return None
    name = None
    if os.path.isabs(cwd):
        try:
            from core import detector

            name = detector.project_for(cwd, cfg)
            if name == getattr(detector, "UNKNOWN_PROJECT", "unknown"):
                name = None
        except Exception:  # noqa: BLE001 - a broken detector must not lose the record
            name = None
    if isinstance(name, str) and name.strip():
        return name
    return os.path.basename(os.path.normpath(cwd)) or None


# ---- internals ----------------------------------------------------------------------------------

def _event(payload: Mapping[str, Any]) -> Optional[str]:
    name = payload.get("hook_event_name")
    if name in EVENTS:
        return name
    if name is None:  # an older or trimmed payload: StopFailure is the one that carries `error`
        return "StopFailure" if "error" in payload else "Stop"
    return None  # some other hook event: not ours to record


def _load_cfg(env: Mapping[str, str]) -> Any:
    from core import config

    try:
        return config.load_config(environ=env)
    except Exception:  # noqa: BLE001 - a broken config.toml must not lose the stop record
        return config.Config(home=config.resolve_home(None, env),
                             projects_dir=config.default_projects_dir(env))


def _run(stdin_bytes: Any, cfg: Any, now: Any, environ: Optional[Mapping[str, str]]) -> None:
    payload = parse_payload(stdin_bytes)
    if payload is None:
        return
    env = os.environ if environ is None else environ
    if cfg is None:
        cfg = _load_cfg(env)
    record = make_record(payload, cfg, now)
    if record is None:
        return
    from core import ledger

    ledger.append_jsonl(os.path.join(str(cfg.home), STOPS_FILE), record)
