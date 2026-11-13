#!/usr/bin/env python3
"""Build a made-up Claude Code history for screenshots and recordings.

    python3 demo/make_demo.py <target-dir> [hero|full]

Creates under <target-dir>: a fake HOME with `code/<project>` git repos, a `projects/` folder of
synthetic session files (the shape Claude Code writes), a Ghosted home, and `demo.tape` for
`vhs` (https://github.com/charmbracelet/vhs). Nothing here reads a real session: every word in
the recording is written in this file. Standard library only.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO = Path(__file__).resolve().parent.parent
MARKER = ".ghosted-demo"  # written into the target folder; a rebuild deletes only a folder that has it
NOW = datetime.now(timezone.utc).replace(microsecond=0)
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="Sam", GIT_AUTHOR_EMAIL="sam@example.invalid",
               GIT_COMMITTER_NAME="Sam", GIT_COMMITTER_EMAIL="sam@example.invalid")

# A believable week: 14 sessions in 5 projects (about what one person starts in a week), three of
# them left waiting. The launch video shows these same numbers.
QUIET = ("landing-page", "ios-app")
CLEAN = {"shop-redesign": 2, "billing-api": 2, "docs-site": 1, "landing-page": 2, "ios-app": 2}


def iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd)] + list(args), check=True, env=GIT_ENV,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def text(words: str) -> Dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": words}]}}


def tool(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_" + name, "name": name, "input": payload}]}}


def limit(resets: datetime) -> Dict[str, Any]:
    return {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "apiErrorStatus": 429,
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "You've hit your session limit · resets 6am"}]},
            "quotaLimits": {"rateLimitType": "five_hour", "resetsAt": int(resets.timestamp()),
                            "status": "rejected"}}


class Demo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.code = self.home / "code"
        self.projects = root / "projects"
        self.le_home = root / "ghosted-home"
        self.bin = root / "bin"

    def repo(self, name: str, uncommitted: int = 0, unpushed: int = 0) -> Path:
        """A git repo with a remote, then ``unpushed`` extra commits and ``uncommitted`` new files."""
        path = self.code / name
        path.mkdir(parents=True)
        origin = self.root / "remotes" / (name + ".git")
        origin.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True, env=GIT_ENV)
        git(path, "init", "-q", "-b", "main")
        (path / "README.md").write_text("# %s\n" % name)
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "start")
        git(path, "remote", "add", "origin", str(origin))
        git(path, "push", "-q", "-u", "origin", "main")
        for i in range(unpushed):
            (path / ("change-%d.txt" % i)).write_text("x\n")
            git(path, "add", "-A")
            git(path, "commit", "-q", "-m", "change %d" % i)
        for i in range(uncommitted):
            (path / ("draft-%d.txt" % i)).write_text("x\n")
        return path

    def session(self, project: str, entries: List[Dict[str, Any]], ended: datetime) -> str:
        sid = str(uuid.uuid4())
        launch = str(self.code / project)
        folder = self.projects / ("-" + launch.strip("/").replace("/", "-").replace(".", "-"))
        folder.mkdir(parents=True, exist_ok=True)
        head = {"isSidechain": False, "userType": "external", "cwd": launch, "sessionId": sid,
                "version": "2.1.319", "entrypoint": "cli"}
        start = ended - timedelta(minutes=10)
        lines = [dict(head, type="user", uuid=str(uuid.uuid4()), timestamp=iso(start),
                      message={"role": "user", "content": "Please work on the %s task." % project})]
        for i, entry in enumerate(entries):
            stamp = ended if i == len(entries) - 1 else start + timedelta(minutes=1 + i)
            lines.append(dict(head, uuid=str(uuid.uuid4()), timestamp=iso(stamp), **entry))
        path = folder / (sid + ".jsonl")
        path.write_text("".join(json.dumps(e) + "\n" for e in lines))
        os.utime(str(path), (ended.timestamp(), ended.timestamp()))
        return sid

    def clean(self, project: str, ended: datetime) -> None:
        self.session(project, [text("Done: the tests pass and the branch is pushed.")], ended)

    def build(self) -> None:
        marker = self.root / MARKER
        if self.root.exists():
            # Rebuilding replaces the folder, so only ever a folder this script made (or an empty one).
            if not self.root.is_dir() or (not marker.is_file() and any(self.root.iterdir())):
                raise SystemExit("%s exists and was not made by this script: give a new or empty folder"
                                 % self.root)
            shutil.rmtree(str(self.root))
        self.projects.mkdir(parents=True)
        marker.write_text("Made by demo/make_demo.py. Safe to delete.\n")
        self.le_home.mkdir(mode=0o700)
        self.bin.mkdir()
        os.symlink(str(REPO / "bin" / "ghosted"), str(self.bin / "ghosted"))
        # The demo lives in a temp folder on most machines, so the temp-folder skip is off here.
        (self.le_home / "config.toml").write_text(
            'workspace_roots = ["%s"]\n[skip]\ntemp_folders = false\n' % self.code)

        def ago(**kw: Any) -> datetime:
            return NOW - timedelta(**kw)

        self.repo("shop-redesign", uncommitted=4)
        self.repo("billing-api", uncommitted=23, unpushed=2)
        self.repo("docs-site", uncommitted=2)
        self.session("shop-redesign", [text(
            "I looked at both layouts. Keep the sticky cart, or move it to the header?")], ago(days=4, hours=2))
        # Ran out of usage mid-task three days ago; the limit reset hours later and nobody came back.
        self.session("billing-api", [text("Working through the webhook retries now."),
                                     limit(ago(days=3, hours=1))], ago(days=3, hours=6))
        self.session("docs-site", [text("Here is the plan."), tool("ExitPlanMode", {
            "plan": "# Move the guides to the new sidebar\n\n1. Regroup the pages\n2. Redirect the old links"})],
            ago(days=6))
        self.session("landing-page", [text(
            "I updated the three tests and the fixtures. Want me to run the whole suite?")], ago(days=2))
        self.session("ios-app", [text(
            "The icons are exported at all three sizes. Want me to tidy the asset names too?")], ago(days=4))
        n = 0
        for project, count in CLEAN.items():
            for _ in range(count):
                n += 1
                self.clean(project, ago(hours=8 + (n * 7) % 130, minutes=n * 17))
        # The first report ever looks back 30 days and says so; run it once here so the recording
        # shows an ordinary week.
        subprocess.run(["ghosted", "report", "--no-quotes"], env=dict(self.env(), TERM="dumb"), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def env(self) -> Dict[str, str]:
        return {"HOME": str(self.home), "GHOSTED_HOME": str(self.le_home),
                "GHOSTED_PROJECTS_DIR": str(self.projects),
                "PATH": "%s:/usr/bin:/bin" % self.bin}

    def tape(self, variant: str, out: Path) -> Path:
        lines = [
            "Output %s" % json.dumps(str(out)),
            "Set Shell bash", "Set FontSize 17", "Set Width 1100",
            "Set Height %d" % (1080 if variant == "full" else 590),
            "Set Padding 28", "Set TypingSpeed 70ms", 'Set Theme "Catppuccin Mocha"',
        ]
        # The demo environment is set inside the shell, hidden: `Env PATH` would also hide the
        # recorder's own helpers from vhs.
        exports = " ".join("%s='%s'" % (k, v) for k, v in self.env().items())
        lines += ["Hide", "Type %s" % json.dumps("export %s PS1='$ ' && cd ~ && clear" % exports),
                  "Enter", "Sleep 500ms", "Show", "Sleep 800ms",
                  'Type "ghosted report"', "Sleep 600ms", "Enter", "Sleep 7s"]
        if variant == "full":
            lines += ['Type "ghosted report --soft --plain"', "Sleep 600ms", "Enter", "Sleep 7s"]
        path = self.root / ("%s.tape" % variant)
        path.write_text("\n".join(lines) + "\n")
        return path


def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    variant = argv[2] if len(argv) > 2 else "hero"
    demo = Demo(Path(argv[1]).resolve())
    demo.build()
    out = Path(argv[3]).resolve() if len(argv) > 3 else demo.root / ("%s.gif" % variant)
    tape = demo.tape(variant, out)
    print("demo data: %s" % demo.root)
    print("record:    vhs %s" % tape)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
