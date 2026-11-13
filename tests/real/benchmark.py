#!/usr/bin/env python3
"""Local benchmark: classify real Claude Code sessions with the detector's rules.

    python3 tests/real/benchmark.py [--since 2026-09-01] [--projects-dir DIR] [--diag]

Prints only ids (8-char session uuid, a 6-hex hash of the project folder name), dates, verdicts,
reason codes and counts (contract section 1). It NEVER prints transcript text, paths or project
names: a folder name is the launch directory, which can name a client. To see which project a row
is, look the uuid up yourself: ls ~/.claude/projects/*/<uuid8>*.jsonl. Manifest fixtures
(the real-session manifest (~/.ghosted/fixtures/real-manifest.json)) are classified on their labelled line cut instead of the live
file. Spec target: flag <= 15 sessions and include both manifest positives, both in the
*waiting* tier (contract 16.1: a `tier` column, waiting / soft counts by reason, how many flags moved
to soft, and each positive's tier). Still ids, dates, verdicts, reasons, tiers and counts only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import classify as C  # noqa: E402
from core import detector as D  # noqa: E402
from core import transcript as T  # noqa: E402

from core import config as CFG  # noqa: E402

# Private, outside the repo: GHOSTED_REAL_MANIFEST, else <GHOSTED_HOME>/fixtures/real-manifest.json.
MANIFEST = CFG.real_manifest_path()
# The detector's own automated rule (core.detector.is_automated) with the shipped [skip]
# defaults: an sdk-cli entrypoint AND at most one human turn in the head, so the benchmark and
# the detector can never drift apart.
SKIP_DEFAULTS = SimpleNamespace(skip_entrypoints=["sdk-cli"], skip_first_prompt_patterns=[])


def default_projects_dir() -> Path:
    env = os.environ.get("GHOSTED_PROJECTS_DIR")
    return Path(env).expanduser() if env else Path("~/.claude/projects").expanduser()


def folder_id(folder: str) -> str:
    """A content-free id for a project folder: rows from one project share it, the name stays hidden."""
    return "p" + hashlib.sha1(folder.encode("utf-8", "surrogatepass")).hexdigest()[:6]


def _local_date(ts: Optional[str], mtime: Optional[float]) -> str:
    if ts:
        try:
            dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                return dt.astimezone().strftime("%Y-%m-%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    if mtime is not None:
        return time.strftime("%Y-%m-%d", time.localtime(mtime))
    return "?"


def read_prefix(path: Path, cut: int) -> List[bytes]:
    out: List[bytes] = []
    with open(path, "rb") as f:
        for line in f:
            if len(out) >= cut:
                break
            out.append(line)
    return out


def load_fixtures() -> List[dict]:
    try:
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [fx for fx in data.get("fixtures", []) if fx.get("labelled_by") != "pending"]


def _bucket(n: int, edges: Tuple[int, ...]) -> str:
    for e in edges:
        if n <= e:
            return "<=%d" % e
    return ">%d" % edges[-1]


def _last_real_qmark_distance(text: str) -> Optional[int]:
    """Characters between the last real '?' and the end of the normalised text (no text exposed)."""
    t = C.normalize_tail(text)
    masked = C.masked_text(t)
    i = max(masked.rfind("?"), masked.rfind("\uff1f"))
    return None if i < 0 else len(t) - 1 - i


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Classify real sessions; prints ids and verdicts only.")
    ap.add_argument("--since", default="2026-09-01", help="local date; sessions modified on or after it")
    ap.add_argument("--projects-dir", default=None, help="default: $GHOSTED_PROJECTS_DIR or ~/.claude/projects")
    ap.add_argument("--diag", action="store_true", help="content-free diagnostics for flagged sessions")
    args = ap.parse_args(argv)

    projects_dir = Path(args.projects_dir).expanduser() if args.projects_dir else default_projects_dir()
    since_ts = time.mktime(time.strptime(args.since, "%Y-%m-%d"))

    rows: List[dict] = []
    fixture_status: Dict[str, str] = {}
    fixture_tier: Dict[str, Tuple[str, str]] = {}  # uuid -> (tier, reason), for the positives line
    seen: set = set()

    for fx in load_fixtures():
        uuid, folder, cut = fx["uuid"], fx["folder"], int(fx["cut"])
        path = projects_dir / folder / (uuid + ".jsonl")
        seen.add(uuid)
        if not path.is_file():
            fixture_status[uuid] = "absent"
            continue
        try:
            lines = read_prefix(path, cut)
            sha_ok = len(lines) == cut and hashlib.sha1(b"".join(lines)).hexdigest() == fx.get("prefix_sha1")
            summary = T.parse_lines(lines, uuid, path=str(path))
        except Exception as exc:  # the type name only: a message could quote the transcript
            fixture_status[uuid] = "error-" + type(exc).__name__
            rows.append({"error": type(exc).__name__, "uuid": uuid, "folder": folder})
            continue
        rows.append({"summary": summary, "folder": folder, "fixture": fx, "sha_ok": sha_ok})

    for path in sorted(projects_dir.glob("*/*.jsonl")):
        if not T.is_session_file(path):
            continue
        uuid = path.stem
        if uuid in seen:
            continue
        try:
            if path.stat().st_mtime < since_ts:
                continue
            summary = T.parse_transcript(path)
        except Exception as exc:  # the type name only: a message could quote the transcript
            rows.append({"error": type(exc).__name__, "uuid": uuid, "folder": path.parent.name})
            continue
        rows.append({"summary": summary, "folder": path.parent.name, "fixture": None, "sha_ok": True})

    table: List[Tuple[str, ...]] = []
    verdicts: Counter = Counter()
    reasons: Dict[str, Counter] = {}
    tiers: Dict[str, Counter] = {C.WAITING: Counter(), C.SOFT: Counter()}
    flagged_texts: List[Tuple[str, str]] = []  # (reason, text) kept in memory for diagnostics only
    for row in rows:
        if "error" in row:
            verdict, reason, uuid, last = "error", row["error"], row["uuid"], "?"
            mark = ""
            tier = None
        else:
            s = row["summary"]
            uuid = s.session_uuid
            last = _local_date(s.last_ts, s.mtime)
            auto = D.is_automated(s, SKIP_DEFAULTS)
            if auto is not None:
                verdict, reason = C.AUTOMATED, auto
            else:
                verdict, reason = C.classify(s, C.DEFAULT_IGNORE_TAIL_PATTERNS)
                if verdict == C.OPEN_QUESTION and s.final_text:
                    flagged_texts.append((reason, s.final_text))
            unsaved = None
            if reason == C.SAVE_OFFER:
                from core import evidence as E
                unsaved = E.unsaved(E.git_facts(s.launch_cwd)) if s.launch_cwd else None
            tier = C.tier(verdict, reason, unsaved=unsaved)
            mark = ""
            fx = row["fixture"]
            if fx is not None:
                fixture_tier[uuid] = (tier or "-", reason)
                expect = fx.get("expect", {}).get("verdict")
                ok = row["sha_ok"] and verdict == expect
                fixture_status[uuid] = "ok" if ok else ("prefix-changed" if not row["sha_ok"] else "got-" + verdict)
                mark = "fixture:" + ("ok" if ok else fixture_status[uuid])
        verdicts[verdict] += 1
        reasons.setdefault(verdict, Counter())[reason] += 1
        if tier is not None:
            tiers[tier][reason] += 1
        table.append((uuid[:8], folder_id(row["folder"]), last, verdict, reason, tier or "-", mark))

    table.sort(key=lambda r: (r[2], r[0]))
    print("%-8s  %-7s  %-10s  %-13s  %-20s  %-7s  %s" % ("uuid", "folder", "last", "verdict", "reason", "tier", ""))
    for r in table:
        print("%-8s  %-7s  %-10s  %-13s  %-20s  %-7s  %s" % r)

    def counts(c: Counter) -> str:
        return ", ".join("%s %d" % (k, n) for k, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))

    def by_reason(v: str) -> str:
        return counts(reasons.get(v, Counter()))

    total = sum(verdicts.values())
    automated = verdicts[C.AUTOMATED]
    flagged = verdicts[C.USAGE_LIMIT] + verdicts[C.OPEN_QUESTION]
    positives = [fx for fx in load_fixtures() if fx.get("expect", {}).get("verdict") in C.FLAGGED_VERDICTS]
    pos_ok = [fx for fx in positives if fixture_status.get(fx["uuid"]) == "ok"]
    print("---")
    print("sessions: %d (modified since %s, plus manifest fixtures)" % (total, args.since))
    print("automated: %d" % automated)
    print("interactive: %d" % (total - automated))
    print("flagged: %d" % flagged)
    print("  usage_limit %d: %s" % (verdicts[C.USAGE_LIMIT], by_reason(C.USAGE_LIMIT) or "-"))
    print("  open_question %d: %s" % (verdicts[C.OPEN_QUESTION], by_reason(C.OPEN_QUESTION) or "-"))
    for v in (C.CLEAN, C.API_ERROR, C.EMPTY, "error"):
        if verdicts[v]:
            print("%s %d: %s" % (v, verdicts[v], by_reason(v)))
    n_waiting = sum(tiers[C.WAITING].values())
    n_soft = sum(tiers[C.SOFT].values())
    print("waiting %d: %s" % (n_waiting, counts(tiers[C.WAITING]) or "-"))
    print("soft %d: %s" % (n_soft, counts(tiers[C.SOFT]) or "-"))
    print("moved to soft: %d of %d flagged" % (n_soft, flagged))
    included = bool(positives) and len(pos_ok) == len(positives)
    print("positives included: %s (%d/%d manifest positives flagged as expected)" % ("yes" if included else "no", len(pos_ok), len(positives)))
    for fx in positives:
        if fixture_status.get(fx["uuid"]) != "ok":
            print("  fixture %s: %s" % (fx["uuid"][:8], fixture_status.get(fx["uuid"], "not run")))
    pos_waiting = 0
    for fx in positives:
        tier, reason = fixture_tier.get(fx["uuid"], ("not run", "-"))
        pos_waiting += tier == C.WAITING
        print("  positive %s: %s (%s)" % (fx["uuid"][:8], tier, reason))
    all_waiting = bool(positives) and pos_waiting == len(positives)
    print("positives in waiting: %s (%d/%d)" % ("yes" if all_waiting else "no", pos_waiting, len(positives)))
    print("target (flag <= 15, both positives, both positives in waiting): %s"
          % ("PASS" if flagged <= 15 and included and all_waiting else "MISS"))

    if args.diag and flagged_texts:
        print("--- diagnostics (open_question only; lengths and counts, no text)")
        lengths = Counter(_bucket(len(t.strip()), (100, 200, 400, 800, 1600, 3200)) for _, t in flagged_texts)
        print("final_text length: %s" % ", ".join("%s %d" % kv for kv in sorted(lengths.items())))
        question = (C.TRAILING_Q, C.OFFER_Q, C.SAVE_OFFER)
        ends_q = sum(1 for r, t in flagged_texts if r in question and C.normalize_tail(t).endswith(("?", "\uff1f")))
        n_tq = sum(1 for r, _ in flagged_texts if r in question)
        print("trailing_q/offer_q ending in '?': %d of %d" % (ends_q, n_tq))
        dist = Counter(_bucket(d, (0, 10, 50, 100, 200)) for d in (_last_real_qmark_distance(t) for r, t in flagged_texts if r in question) if d is not None)
        print("chars after the last real '?': %s" % ", ".join("%s %d" % kv for kv in sorted(dist.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
