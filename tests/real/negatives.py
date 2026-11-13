#!/usr/bin/env python3
"""Pick and hand-label the ten negative (clean) real-session fixtures.

    python3 tests/real/negatives.py pick [--n 12] [--since 2026-09-01]   # content-free; safe anywhere
    python3 tests/real/negatives.py label                                 # SHOWS TEXT: run it yourself,
                                                                          # in a real terminal only

`pick` chooses sessions the classifier calls clean, spread across project folders, and always tries
to include one whose ending is "Safe to clear" and one short Q&A session (spec section 7). It adds them
to the real-session manifest (~/.ghosted/fixtures/real-manifest.json) as `labelled_by: "pending"` with a line cut and prefix hash, and
prints only 8-char ids, folder hashes and booleans.

`label` is the hand-labelling step. It prints the last part of each pending session's final assistant
message so a person can judge it, so it refuses to run unless stdin and stdout are both a terminal:
that keeps transcript text out of any Claude session (a `!` command or the Bash tool has no TTY).
Answers: y = clean (nothing was left waiting), n = not clean (it was waiting on you), s = skip.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import classify as C  # noqa: E402
from core import transcript as T  # noqa: E402

from core import config as CFG  # noqa: E402

# Private, outside the repo: GHOSTED_REAL_MANIFEST, else <GHOSTED_HOME>/fixtures/real-manifest.json.
MANIFEST = CFG.real_manifest_path()
ACTIVE_SECONDS = 30 * 60


def projects_dir() -> Path:
    env = os.environ.get("GHOSTED_PROJECTS_DIR")
    return Path(env).expanduser() if env else Path("~/.claude/projects").expanduser()


def folder_hash(folder: str) -> str:
    return hashlib.sha1(folder.encode("utf-8")).hexdigest()[:6]


def load_manifest() -> Dict:
    with open(MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def save_manifest(data: Dict) -> None:
    MANIFEST.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = MANIFEST.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, MANIFEST)


def prefix_facts(path: Path) -> Tuple[int, str]:
    """Line count and sha1 of the whole file as it is now (the cut is the current end)."""
    h = hashlib.sha1()
    n = 0
    with open(path, "rb") as f:
        for line in f:
            n += 1
            h.update(line)
    return n, h.hexdigest()


def shape(path: Path) -> Tuple[int, int]:
    """(human turns, assistant tool_use blocks) for the whole session. Content-free."""
    humans = tools = 0
    with open(path, "rb") as f:
        for raw in f:
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain"):
                continue
            if T.human_turn(entry):
                humans += 1
            if entry.get("type") == "assistant":
                content = (entry.get("message") or {}).get("content")
                if isinstance(content, list):
                    tools += sum(1 for b in content if isinstance(b, dict) and b.get("type") in T.TOOL_USE_TYPES)
    return humans, tools


def cmd_pick(args: argparse.Namespace) -> int:
    data = load_manifest()
    known = {fx["uuid"] for fx in data["fixtures"]}
    since = time.mktime(time.strptime(args.since, "%Y-%m-%d"))
    now = time.time()
    pool: List[Dict] = []
    for path in sorted(projects_dir().glob("*/*.jsonl")):
        if not T.is_session_file(path):
            continue
        uuid = path.stem
        st = path.stat()
        if uuid in known or st.st_mtime < since or now - st.st_mtime < ACTIVE_SECONDS:
            continue
        try:
            summary = T.parse_transcript(path)
        except Exception as exc:  # noqa: BLE001 - report the type only
            print(f"skip {uuid[:8]}: {type(exc).__name__}")
            continue
        if summary.entrypoints & {"sdk-cli"}:
            continue
        ending = C.classify(summary, C.DEFAULT_IGNORE_TAIL_PATTERNS)
        if ending.verdict != C.CLEAN:
            continue
        tail = C.normalize_tail(summary.final_text or "").lower()
        humans, tools = shape(path)
        pool.append({
            "path": path, "uuid": uuid, "folder": path.parent.name, "mtime": st.st_mtime,
            "safe_to_clear": tail.endswith("safe to clear.") or tail.endswith("safe to clear"),
            "qa": humans <= 3 and tools == 0, "humans": humans, "reason": ending.reason,
        })

    chosen: List[Dict] = []
    def take(pred) -> None:
        for c in pool:
            if c not in chosen and pred(c):
                chosen.append(c)
                return
    take(lambda c: c["safe_to_clear"])
    take(lambda c: c["qa"])
    # Then spread across folders, newest first, one per folder per round.
    by_folder: Dict[str, List[Dict]] = {}
    for c in sorted(pool, key=lambda c: -c["mtime"]):
        by_folder.setdefault(c["folder"], []).append(c)
    while len(chosen) < args.n and any(by_folder.values()):
        for folder in sorted(by_folder):
            if by_folder[folder] and len(chosen) < args.n:
                c = by_folder[folder].pop(0)
                if c not in chosen:
                    chosen.append(c)

    for c in chosen:
        cut, sha = prefix_facts(c["path"])
        data["fixtures"].append({
            "uuid": c["uuid"], "folder": c["folder"], "cut": cut, "prefix_sha1": sha,
            "expect": {"verdict": "clean"},
            "note": "negative candidate" + ("; ends 'Safe to clear'" if c["safe_to_clear"] else "")
                    + ("; Q&A" if c["qa"] else ""),
            "labelled_by": "pending",
        })
        print(f"pending {c['uuid'][:8]} folder#{folder_hash(c['folder'])} reason={c['reason']} "
              f"safe_to_clear={c['safe_to_clear']} qa={c['qa']}")
    save_manifest(data)
    print(f"added {len(chosen)} pending negatives (pool {len(pool)}); "
          f"safe_to_clear found={any(c['safe_to_clear'] for c in chosen)} qa found={any(c['qa'] for c in chosen)}")
    return 0


def tail_text(fx: Dict) -> Optional[str]:
    path = projects_dir() / fx["folder"] / (fx["uuid"] + ".jsonl")
    if not path.exists():
        return None
    with open(path, "rb") as f:
        lines = [line for _, line in zip(range(fx["cut"]), f)]
    summary = T.parse_lines(lines, fx["uuid"], str(path))
    return summary.final_text


def cmd_label(args: argparse.Namespace) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.stderr.write("label shows transcript text, so it only runs in a real terminal "
                         "(not inside Claude Code). Open Terminal and run it there.\n")
        return 2
    data = load_manifest()
    pending = [fx for fx in data["fixtures"] if fx.get("labelled_by") == "pending"]
    if not pending:
        print("Nothing pending.")
        return 0
    print(f"{len(pending)} sessions to label. For each: was anything left waiting on you when it ended?\n")
    for i, fx in enumerate(pending, 1):
        text = tail_text(fx)
        print("=" * 72)
        print(f"[{i}/{len(pending)}] {fx['folder']}  session {fx['uuid'][:8]}  ({fx.get('note', '')})")
        print("-" * 72)
        print((text or "(no final text)")[-600:])
        print("-" * 72)
        while True:
            ans = input("Clean, nothing waiting on you? [y]es / [n]o / [s]kip / [q]uit: ").strip().lower()
            if ans in ("y", "n", "s", "q"):
                break
        if ans == "q":
            break
        if ans == "s":
            continue
        fx["labelled_by"] = "me"
        fx["labelled_at"] = time.strftime("%Y-%m-%d")
        if ans == "n":
            fx["expect"] = {"verdict": "open_question"}
            fx["note"] = (fx.get("note", "") + "; me: it was waiting on me (classifier said clean)").lstrip("; ")
        save_manifest(data)
    done = sum(1 for fx in data["fixtures"] if fx.get("labelled_by") not in (None, "", "pending"))
    print(f"\nSaved. {done} sessions labelled by you. Tell Claude 'labels done'.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("pick")
    pk.add_argument("--n", type=int, default=12)
    pk.add_argument("--since", default="2026-09-01")
    sub.add_parser("label")
    args = p.parse_args(argv)
    return cmd_pick(args) if args.cmd == "pick" else cmd_label(args)


if __name__ == "__main__":
    sys.exit(main())
