"""directory/: the code-free plugin listed in Claude's plugin directory (contract 16.22).

It must stay in step with the main plugin (same name, version, description, links and skill
description), hold only the file types the directory accepts from an unverified organization, and
pin the uvx line to the release tag of the current version.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

import core

ROOT = Path(__file__).resolve().parent.parent
LISTING = ROOT / "directory"
ALLOWED_SUFFIXES = {".md", ".json", ".png"}


def front_matter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    block = text.split("---", 2)[1]
    out = {}
    for line in block.strip().splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


class ListingTests(unittest.TestCase):
    def test_manifest_matches_the_main_plugin(self) -> None:
        main = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        listing = json.loads((LISTING / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(listing.pop("skills"), "./skills/")
        main.pop("skills")
        self.assertEqual(listing, main)
        self.assertEqual(listing["version"], core.__version__)

    def test_skill_description_matches_and_pre_approves_nothing(self) -> None:
        main = front_matter(ROOT / "core" / "skills" / "ghosted" / "SKILL.md")
        listing = front_matter(LISTING / "skills" / "ghosted" / "SKILL.md")
        for key in ("name", "description", "argument-hint"):
            self.assertEqual(listing[key], main[key], key)
        self.assertNotIn("allowed-tools", listing)    # the user approves the command

    def test_uvx_line_is_pinned_to_this_release(self) -> None:
        tag = "v" + core.__version__
        for path in (LISTING / "skills" / "ghosted" / "SKILL.md", LISTING / "README.md"):
            pins = re.findall(r"github\.com/byndp/ghosted@([^\s`]+) ghosted", path.read_text())
            self.assertTrue(pins, path)
            self.assertEqual(set(pins), {tag}, path)

    def test_only_files_the_directory_accepts(self) -> None:
        for path in LISTING.rglob("*"):
            if path.is_dir():
                continue
            rel = path.relative_to(LISTING).as_posix()
            self.assertTrue(path.suffix in ALLOWED_SUFFIXES or rel == "LICENSE", rel)
            self.assertFalse(path.read_bytes().startswith(b"#!"), rel)
        self.assertEqual((LISTING / ".claude-plugin" / "icon.png").read_bytes(),
                         (ROOT / ".claude-plugin" / "icon.png").read_bytes())
        self.assertEqual((LISTING / "LICENSE").read_text(), (ROOT / "LICENSE").read_text())


if __name__ == "__main__":
    unittest.main()
