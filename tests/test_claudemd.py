"""The opt-in session rules in ~/.claude/CLAUDE.md (core.claudemd, contract 16.11 part C).

Temp homes only; nothing reads the real ~/.claude.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List

from core import claudemd, cli, install, onboard
from tests.test_onboard import Base as OnboardBase

NOW = datetime(2026, 11, 8, 18, 0, tzinfo=timezone.utc)
MINE = "# My rules\n\nAlways write tests first.\nNever push on Fridays.\n"
REPO = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.le_home = self.root / "ghosted"
        self.ctx = SimpleNamespace(home=self.home, le_home=self.le_home, backups_dir=self.le_home / "backups", now=NOW)
        self.path = self.home / ".claude" / "CLAUDE.md"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def backups(self) -> List[Path]:
        d = self.le_home / "backups"
        return sorted(d.glob("CLAUDE.md.*")) if d.is_dir() else []


class BlockTests(Base):
    def test_append_once_then_rerun_changes_nothing(self) -> None:
        self.path.write_text(MINE)
        step = claudemd.add(self.ctx, ["le_add"])
        self.assertTrue(step.ok and step.changed)
        text = self.path.read_text()
        self.assertTrue(text.startswith(MINE + "\n" + claudemd.BEGIN + "\n"))
        self.assertTrue(text.endswith(claudemd.END + "\n"))
        self.assertIn(claudemd.text_of("le_add"), text)
        self.assertNotIn(claudemd.text_of("edit_tools"), text)
        self.assertEqual(claudemd.present(self.path), {"le_add"})
        again = claudemd.add(self.ctx, ["le_add"])
        self.assertEqual((again.action, again.changed), ("exists", False))
        self.assertEqual(self.path.read_text(), text)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.backups()[0].read_text(), MINE)
        self.assertEqual(os.stat(str(self.backups()[0])).st_mode & 0o777, 0o600)

    def test_second_rule_joins_the_same_block_in_order(self) -> None:
        self.path.write_text(MINE)
        claudemd.add(self.ctx, ["edit_tools"])
        claudemd.add(self.ctx, ["le_add"])
        text = self.path.read_text()
        self.assertEqual(text.count(claudemd.BEGIN), 1)
        self.assertLess(text.index(claudemd.text_of("le_add")), text.index(claudemd.text_of("edit_tools")))
        self.assertEqual(claudemd.present(self.path), {"le_add", "edit_tools"})

    def test_block_in_the_middle_is_rewritten_in_place(self) -> None:
        self.path.write_text(MINE)
        claudemd.add(self.ctx, ["le_add"])
        with open(str(self.path), "a") as f:
            f.write("\n## Written after the block\n")
        claudemd.add(self.ctx, ["edit_tools"])
        text = self.path.read_text()
        self.assertTrue(text.endswith("\n## Written after the block\n"))
        self.assertTrue(text.startswith(MINE))

    def test_uninstall_removes_only_the_block(self) -> None:
        self.path.write_text(MINE)
        mode = 0o640
        os.chmod(str(self.path), mode)
        claudemd.add(self.ctx, ["le_add", "edit_tools"])
        self.assertEqual(os.stat(str(self.path)).st_mode & 0o777, mode)
        dry = claudemd.remove(self.ctx, dry_run=True)
        self.assertTrue(dry.changed)
        self.assertIn(claudemd.BEGIN, self.path.read_text())
        step = claudemd.remove(self.ctx)
        self.assertEqual((step.action, step.changed), ("remove", True))
        self.assertEqual(self.path.read_bytes(), MINE.encode())
        self.assertEqual(claudemd.remove(self.ctx).action, "absent")

    def test_missing_file_is_created_and_removed_again(self) -> None:
        step = claudemd.add(self.ctx, ["edit_tools"])
        self.assertEqual(step.action, "create")
        self.assertTrue(self.path.read_text().startswith(claudemd.BEGIN))
        self.assertEqual(self.backups(), [])
        claudemd.remove(self.ctx)
        self.assertFalse(self.path.exists())

    def test_symlink_is_written_through(self) -> None:
        real = self.root / "dotfiles" / "CLAUDE.md"
        real.parent.mkdir()
        real.write_text(MINE)
        self.path.symlink_to(real)
        claudemd.add(self.ctx, ["le_add"])
        self.assertTrue(self.path.is_symlink())
        self.assertIn(claudemd.BEGIN, real.read_text())
        claudemd.remove(self.ctx)
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(real.read_text(), MINE)

    def test_steps_never_print_the_file(self) -> None:
        self.path.write_text(MINE)
        lines = [claudemd.add(self.ctx, ["le_add"]).line(self.home), claudemd.remove(self.ctx).line(self.home)]
        for line in lines:
            self.assertNotIn("Fridays", line)
            self.assertIn("~/.claude/CLAUDE.md", line)

    def test_unbalanced_markers_are_refused_and_nothing_changes(self) -> None:
        # An orphan begin marker must never pair with a later end marker.
        cases = [MINE + claudemd.BEGIN + "\n## Their own rule\nkeep me\n",
                 MINE + claudemd.END + "\n",
                 MINE + claudemd.END + "\nmiddle\n" + claudemd.BEGIN + "\n",
                 MINE + "\n" + claudemd.render(["le_add"]) + "\nx\n" + claudemd.render(["edit_tools"]) + "\n"]
        for text in cases:
            self.path.write_text(text)
            for step in (claudemd.add(self.ctx, ["edit_tools"]), claudemd.remove(self.ctx)):
                self.assertFalse(step.ok, text)
                self.assertEqual(step.action, "refuse")
                self.assertIn("markers", step.detail)
            self.assertEqual(self.path.read_text(), text)
            self.assertEqual(claudemd.present(self.path), set())

    def test_crlf_file_round_trips_byte_for_byte(self) -> None:
        # Line endings outside the block are the user's.
        original = MINE.replace("\n", "\r\n").encode()
        self.path.write_bytes(original)
        claudemd.add(self.ctx, ["le_add", "edit_tools"])
        self.assertTrue(self.path.read_bytes().startswith(original))
        self.assertEqual(claudemd.present(self.path), {"le_add", "edit_tools"})
        claudemd.remove(self.ctx)
        self.assertEqual(self.path.read_bytes(), original)

    def test_unknown_rule_refused(self) -> None:
        with self.assertRaises(ValueError):
            claudemd.add(self.ctx, ["nope"])

    def test_wording_is_for_everyone(self) -> None:
        for key, text in claudemd.RULES:
            for word in ("/Users", "workspace", "!"):
                self.assertNotIn(word, text, key)
            self.assertNotRegex(text, r"\b[A-Z][a-z]+'s\b", key)   # nobody's name: it is anyone's rule
        self.assertIn("ghosted add", claudemd.text_of("le_add"))
        self.assertIn("--public", claudemd.text_of("le_add"))
        self.assertIn("Edit/Write", claudemd.text_of("edit_tools"))

    def test_the_guide_has_the_same_text(self) -> None:
        guide = (REPO / "docs" / "guide.md").read_text()
        for _, text in claudemd.RULES:
            self.assertIn(text, guide)

    def test_the_readme_is_for_everyone(self) -> None:
        readme = (REPO / "README.md").read_text()
        for word in ("/Users/", "Pilot", "pilot", "untested"):
            self.assertNotIn(word, readme)
        self.assertLess(len(readme.splitlines()), 130)           # short: the guide holds the rest
        self.assertIn("uvx --from git+https://github.com/byndp/ghosted ghosted report", readme)
        self.assertIn("ghosted receipt", readme)
        self.assertIn("/plugin install ghosted --marketplace", readme)
        self.assertIn("docs/guide.md", readme)
        self.assertIn("(docs/demo.gif)", readme)                 # the picture above the fold
        self.assertLess(readme.index("docs/demo.gif"), readme.index("## Install"))
        self.assertTrue((REPO / "docs" / "demo.gif").read_bytes().startswith(b"GIF8"))


class CliTests(Base):
    def run_cli(self, *argv: str):
        env = {"HOME": str(self.home), "GHOSTED_HOME": str(self.le_home), "PATH": str(self.root / "nobin"),
               "GHOSTED_PROJECTS_DIR": str(self.root / "projects")}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), environ=env)
        return code, out.getvalue(), err.getvalue()

    def test_uninstall_claude_md(self) -> None:
        self.path.write_text(MINE)
        claudemd.add(self.ctx, ["le_add"])
        code, out, err = self.run_cli("uninstall", "--claude-md", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertIn("would remove", out)
        self.assertIn(claudemd.BEGIN, self.path.read_text())
        code, out, err = self.run_cli("uninstall", "--claude-md")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.path.read_text(), MINE)
        self.assertNotIn("Fridays", out + err)

    def test_uninstall_all_includes_it(self) -> None:
        self.path.write_text(MINE)
        claudemd.add(self.ctx, ["edit_tools"])
        code, out, err = self.run_cli("uninstall", "--all")
        self.assertEqual(self.path.read_text(), MINE)
        self.assertIn("claude-md", out)


class OnboardingTests(OnboardBase):
    def setUp(self) -> None:
        super().setUp()
        self.present: set = set()
        self.added: List[List[str]] = []

    def effects(self) -> onboard.Effects:
        fx = super().effects()
        fx.claude_md_present = lambda: set(self.present)

        def add(keys: List[str]) -> Any:
            self.added.append(list(keys))
            return install.Step("claude-md", "update", str(self.user_home / ".claude" / "CLAUDE.md"), "added",
                                changed=True)

        fx.claude_md_add = add
        return fx

    def test_both_rules_shown_word_for_word_and_asked_once_each(self) -> None:
        code, io_ = self.run_onboard(["", "n", "y", "n", ""])
        self.assertEqual(code, 0, io_.text)
        for _, text in claudemd.RULES:
            for line in text.splitlines():
                if line.strip():
                    self.assertIn(line, "\n".join(io_.said))
        asked = [p for p in io_.prompts if "[y/N]" in p]
        self.assertEqual(len(asked), 2)
        self.assertEqual(self.added, [["le_add"]])
        self.assertIn("ghosted uninstall --claude-md", io_.text)
        self.assertPlain(io_)

    def test_rule_already_there_is_not_asked(self) -> None:
        self.present = {"le_add"}
        code, io_ = self.run_onboard(["", "n", "y", ""])
        self.assertEqual(code, 0, io_.text)
        self.assertEqual(len([p for p in io_.prompts if "[y/N]" in p]), 1)
        self.assertEqual(self.added, [["edit_tools"]])

    def test_both_there_says_so(self) -> None:
        self.present = {"le_add", "edit_tools"}
        code, io_ = self.run_onboard(["", "n", ""])
        self.assertEqual(code, 0, io_.text)
        self.assertTrue(any("already" in s and "CLAUDE.md" in s for s in io_.said))
        self.assertEqual(self.added, [])

    def test_no_to_both_writes_nothing(self) -> None:
        code, io_ = self.run_onboard(["", "n", "n", "n", ""])
        self.assertEqual(code, 0, io_.text)
        self.assertEqual(self.added, [])
        self.assertIn("docs/guide.md", io_.text)

    def test_default_effects_offer_the_real_file(self) -> None:
        fx = onboard.default_effects(self.env, NOW)
        self.assertEqual(fx.claude_md_present(), set())
        step = fx.claude_md_add(["edit_tools"])
        self.assertTrue(step.ok)
        self.assertEqual(claudemd.present(self.user_home / ".claude" / "CLAUDE.md"), {"edit_tools"})


if __name__ == "__main__":
    unittest.main()
