#!/usr/bin/env python3
"""Tests for scripts/crowdin_validator.py.

Stdlib `unittest` only -- this repo has no test dependencies and no package
manifest, so the suite has to run on a bare `python3`:

    python3 -m unittest discover -s scripts -p 'test_*.py' -v

Two layers. `ParsingTests` exercise the pure `.po` parsing/detection helpers
directly. `ValidatorEndToEndTests` build a throwaway git repo (bare origin +
working clone) per case and run the script as a subprocess, because the exit
code and stdout -- not any Python API -- are what Bitrise actually consumes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import crowdin_validator as validator  # noqa: E402

VALIDATOR = Path(__file__).resolve().parent / "crowdin_validator.py"

# Identity for the throwaway repos, passed per-command in the environment
# rather than written with `git config`. A stray `git config` runs against
# whatever repo the process happens to be in, so a bad cwd would silently
# rewrite the real repo's .git/config -- and misattribute its next commit.
# Env vars cannot leak that way.
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}

HEADER = 'msgid ""\nmsgstr ""\n"Language: en-US\\n"\n'


def po(*blocks: str) -> str:
    """Assemble a .po file the way update_strings.py's write_po_file does."""
    return f"{HEADER}\n" + "\n\n".join(blocks) + "\n\n"


def entry(key: str, value: str) -> str:
    return f'msgid "{key}"\nmsgstr "{value}"'


def plural(key: str, *values: str) -> str:
    forms = "\n".join(f'msgstr[{i}] "{v}"' for i, v in enumerate(values))
    return f'msgid "{key}"\nmsgid_plural "{key}"\n{forms}'


class ParsingTests(unittest.TestCase):
    def test_parses_single_line_entries_and_skips_the_header(self):
        entries = validator.parse_entries(po(entry("about", "About"), entry("bible", "Bible")))
        self.assertEqual(sorted(entries), ["about", "bible"])
        self.assertEqual(validator.entry_text(entries["about"]), "About")

    def test_parses_plural_entries(self):
        entries = validator.parse_entries(po(plural("xHours", "1 hour", "%1$s hours")))
        self.assertEqual(list(entries), ["xHours"])
        self.assertEqual(validator.entry_text(entries["xHours"]), "1 hour / %1$s hours")

    def test_entry_text_unescapes(self):
        entries = validator.parse_entries(po(entry("greeting", 'Hi \\"you\\"\\nthere')))
        self.assertEqual(validator.entry_text(entries["greeting"]), 'Hi "you"\nthere')

    def test_editing_one_plural_form_changes_the_body(self):
        before = validator.parse_entries(po(plural("xHours", "1 hour", "%1$s hours")))
        after = validator.parse_entries(po(plural("xHours", "1 hour", "%1$s hrs")))
        self.assertNotEqual(before["xHours"], after["xHours"])

    def test_duplicate_keys_are_counted(self):
        content = po(entry("about", "About"), entry("about", "About Us"), entry("bible", "Bible"))
        self.assertEqual(validator.duplicate_keys(content), [("about", 2)])

    def test_no_duplicates_reported_for_a_clean_file(self):
        self.assertEqual(validator.duplicate_keys(po(entry("about", "About"))), [])

    def test_malformed_detects_multi_line_continuation(self):
        content = po('msgid "blurb"\nmsgstr ""\n"a long line "\n"continued"')
        self.assertEqual(validator.malformed_keys(content), ["blurb"])

    def test_malformed_detects_a_comment_between_msgid_and_msgstr(self):
        content = po('msgid "about"\n#. translator note\nmsgstr "About"')
        self.assertEqual(validator.malformed_keys(content), ["about"])

    def test_malformed_detects_a_missing_msgstr(self):
        content = po('msgid "about"', entry("bible", "Bible"))
        self.assertEqual(validator.malformed_keys(content), ["about"])

    def test_well_formed_entries_are_not_malformed(self):
        content = po(entry("about", "About"), plural("xHours", "1 hour", "%1$s hours"))
        self.assertEqual(validator.malformed_keys(content), [])

    def test_empty_value_keys(self):
        entries = validator.parse_entries(po(entry("about", ""), entry("bible", "Bible")))
        self.assertEqual(validator.empty_value_keys(entries), ["about"])

    def test_detect_renames_pairs_matching_bodies(self):
        gone = validator.parse_entries(po(entry("badges", "Badges")))
        new = validator.parse_entries(po(entry("userBadges", "Badges")))
        self.assertEqual(validator.detect_renames(gone, new), [("badges", "userBadges", gone["badges"])])

    def test_detect_renames_ignores_unrelated_add_and_delete(self):
        gone = validator.parse_entries(po(entry("badges", "Badges")))
        new = validator.parse_entries(po(entry("streaks", "Streaks")))
        self.assertEqual(validator.detect_renames(gone, new), [])

    def test_detect_renames_ignores_a_merge(self):
        # Folding one string's text into another rewrites the surviving body,
        # so there is no identical pair to match -- a merge is a delete plus
        # an edit, never a rename.
        gone = validator.parse_entries(po(entry("welcomeBody", "Glad you're here")))
        new = validator.parse_entries(po(entry("welcomeTitle", "Welcome -- glad you're here")))
        self.assertEqual(validator.detect_renames(gone, new), [])

    def test_detect_renames_pairs_a_merge_that_keeps_one_body_verbatim(self):
        # Known limit of the heuristic: if a merge happens to leave one of the
        # deleted bodies byte-identical, that half is indistinguishable from a
        # rename and is reported as one. The notice is advisory, and the
        # translation consequence it warns about is the same either way.
        gone = validator.parse_entries(po(entry("partA", "Hello"), entry("partB", "World")))
        new = validator.parse_entries(po(entry("merged", "Hello")))
        renames = validator.detect_renames(gone, new)
        self.assertEqual([(old, key) for old, key, _ in renames], [("partA", "merged")])

    def test_detect_renames_claims_each_added_key_once(self):
        gone = validator.parse_entries(po(entry("a", "Same"), entry("b", "Same")))
        new = validator.parse_entries(po(entry("c", "Same")))
        renames = validator.detect_renames(gone, new)
        self.assertEqual([(old, new_key) for old, new_key, _ in renames], [("a", "c")])


class ValidatorEndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        self.repo = root / "work"

        self.git("init", "--bare", "--initial-branch=main", str(self.origin), cwd=root)
        self.git("init", "--initial-branch=main", str(self.repo), cwd=root)
        self.git("remote", "add", "origin", str(self.origin))
        (self.repo / "strings" / "en").mkdir(parents=True)

    def git(self, *args: str, cwd: Path | None = None) -> None:
        subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=str(cwd or self.repo),
            env={**os.environ, **GIT_ENV},
            check=True,
            capture_output=True,
            text=True,
        )

    def write(self, name: str, content: str) -> None:
        (self.repo / "strings" / "en" / name).write_text(content, encoding="utf-8")

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-m", message)

    def commit_base(self, **files: str) -> None:
        for name, content in files.items():
            self.write(f"{name}.po", content)
        self.commit("base")
        self.git("push", "-u", "origin", "main")
        self.git("switch", "-c", "feature")

    def run_validator(self, branch: str | None = None) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("BITRISE_GIT_BRANCH", "GITHUB_HEAD_REF")}
        env["CROWDIN_SYNC_BASE"] = "main"
        if branch is not None:
            env["BITRISE_GIT_BRANCH"] = branch
        return subprocess.run(
            [sys.executable, str(VALIDATOR)],
            cwd=str(self.repo),
            env=env,
            capture_output=True,
            text=True,
        )

    def assertPasses(self, result: subprocess.CompletedProcess) -> str:
        self.assertEqual(result.returncode, 0, msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result.stdout

    def assertFails(self, result: subprocess.CompletedProcess) -> str:
        self.assertEqual(result.returncode, 1, msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        return result.stdout

    # -- notices (exit 0) -------------------------------------------------

    def test_no_po_changes_passes(self):
        self.commit_base(app=po(entry("about", "About")))
        (self.repo / "README.md").write_text("hello\n", encoding="utf-8")
        self.commit("unrelated")
        self.assertIn("no strings changed", self.assertPasses(self.run_validator()))

    def test_addition_passes(self):
        self.commit_base(app=po(entry("about", "About")))
        self.write("app.po", po(entry("about", "About"), entry("bible", "Bible")))
        self.commit("add")
        stdout = self.assertPasses(self.run_validator())
        self.assertIn("1 added, 0 edited, 0 deleted, 0 renamed", stdout)
        self.assertIn("bible", stdout)

    def test_edit_is_reported_but_passes(self):
        self.commit_base(app=po(entry("about", "About")))
        self.write("app.po", po(entry("about", "About Us")))
        self.commit("edit")
        stdout = self.assertPasses(self.run_validator())
        self.assertIn("0 added, 1 edited, 0 deleted, 0 renamed", stdout)
        self.assertIn("unapproved", stdout)
        self.assertIn('"About" -> "About Us"', stdout)

    def test_delete_is_reported_but_passes(self):
        self.commit_base(app=po(entry("about", "About"), entry("bible", "Bible")))
        self.write("app.po", po(entry("about", "About")))
        self.commit("delete")
        stdout = self.assertPasses(self.run_validator())
        self.assertIn("0 added, 0 edited, 1 deleted, 0 renamed", stdout)
        self.assertIn("deleted", stdout)
        self.assertIn("bible", stdout)

    def test_rename_is_reported_separately(self):
        self.commit_base(app=po(entry("badges", "Badges")))
        self.write("app.po", po(entry("userBadges", "Badges")))
        self.commit("rename")
        stdout = self.assertPasses(self.run_validator())
        self.assertIn("0 added, 0 edited, 0 deleted, 1 renamed", stdout)
        self.assertIn("badges -> userBadges", stdout)
        self.assertIn("do NOT carry over", stdout)

    def test_merging_one_string_into_another_passes(self):
        self.commit_base(
            app=po(entry("welcomeTitle", "Welcome"), entry("welcomeBody", "Glad you're here"))
        )
        self.write("app.po", po(entry("welcomeTitle", "Welcome -- glad you're here")))
        self.commit("merge body into title")
        stdout = self.assertPasses(self.run_validator())
        self.assertIn("0 added, 1 edited, 1 deleted, 0 renamed", stdout)
        self.assertIn(
            "welcomeTitle (strings/en/app.po): \"Welcome\" -> \"Welcome -- glad you\'re here\"",
            stdout,
        )
        self.assertIn("welcomeBody (strings/en/app.po): \"Glad you\'re here\"", stdout)
        self.assertNotIn("likely rename(s)", stdout)

    def test_merging_two_strings_into_a_new_key_is_not_a_rename(self):
        self.commit_base(app=po(entry("partA", "Hello"), entry("partB", "World")))
        self.write("app.po", po(entry("greeting", "Hello World")))
        self.commit("merge two into a new key")
        stdout = self.assertPasses(self.run_validator())
        self.assertIn("1 added, 0 edited, 2 deleted, 0 renamed", stdout)
        self.assertNotIn("likely rename(s)", stdout)

    def test_merging_a_string_into_another_file_passes(self):
        self.commit_base(
            app=po(entry("theme", "Theme")),
            settings=po(entry("appearance", "Appearance")),
        )
        self.write("app.po", HEADER + "\n" + entry("unrelated", "Unrelated") + "\n\n")
        self.write("settings.po", po(entry("appearance", "Appearance & theme")))
        self.commit("move theme text into settings")
        stdout = self.assertPasses(self.run_validator())
        self.assertIn("1 added, 1 edited, 1 deleted, 0 renamed", stdout)
        self.assertIn("theme (strings/en/app.po)", stdout)
        self.assertIn("appearance (strings/en/settings.po)", stdout)

    def test_edited_plural_is_reported(self):
        self.commit_base(search=po(plural("xHours", "1 hour", "%1$s hours")))
        self.write("search.po", po(plural("xHours", "1 hour", "%1$s hrs")))
        self.commit("edit plural")
        self.assertIn("1 edited", self.assertPasses(self.run_validator()))

    def test_reordering_entries_is_not_a_change(self):
        self.commit_base(app=po(entry("about", "About"), entry("bible", "Bible")))
        self.write("app.po", po(entry("bible", "Bible"), entry("about", "About")))
        self.commit("reorder")
        stdout = self.assertPasses(self.run_validator())
        self.assertIn("0 added, 0 edited, 0 deleted, 0 renamed", stdout)

    def test_sync_branch_is_skipped_entirely(self):
        self.commit_base(app=po(entry("about", "About"), entry("bible", "Bible")))
        self.write("app.po", po(entry("about", "About")))
        self.commit("bot delete")
        stdout = self.assertPasses(self.run_validator(branch="chore/crowdin-sync"))
        self.assertIn("skipping validation", stdout)

    # -- errors (exit 1) --------------------------------------------------

    def test_cross_file_collision_fails(self):
        self.commit_base(app=po(entry("about", "About")), settings=po(entry("theme", "Theme")))
        self.write("settings.po", po(entry("theme", "Theme"), entry("about", "About")))
        self.commit("collide")
        stdout = self.assertFails(self.run_validator())
        self.assertIn("duplicate an existing key in another file", stdout)
        self.assertIn("app.po", stdout)

    def test_duplicate_key_in_one_file_fails(self):
        self.commit_base(app=po(entry("about", "About")))
        self.write("app.po", po(entry("about", "About"), entry("bible", "Bible"), entry("bible", "Bible!")))
        self.commit("duplicate")
        stdout = self.assertFails(self.run_validator())
        self.assertIn("defined more than once", stdout)
        self.assertIn("bible", stdout)

    def test_malformed_entry_fails(self):
        self.commit_base(app=po(entry("about", "About")))
        self.write("app.po", po(entry("about", "About"), 'msgid "blurb"\nmsgstr ""\n"split "\n"line"'))
        self.commit("malformed")
        self.assertIn("unparseable entry", self.assertFails(self.run_validator()))

    def test_deleting_a_whole_file_fails(self):
        self.commit_base(app=po(entry("about", "About"), entry("bible", "Bible")))
        (self.repo / "strings" / "en" / "app.po").unlink()
        self.commit("drop file")
        stdout = self.assertFails(self.run_validator())
        self.assertIn("whole file(s) removed", stdout)
        self.assertIn("deleted, 2 string(s)", stdout)
        self.assertIn("1 file(s) removed", stdout)

    def test_emptying_a_file_in_place_fails(self):
        self.commit_base(app=po(entry("about", "About"), entry("bible", "Bible")))
        self.write("app.po", HEADER)
        self.commit("empty file")
        stdout = self.assertFails(self.run_validator())
        self.assertIn("whole file(s) removed", stdout)
        self.assertIn("emptied, 2 string(s)", stdout)

    def test_deleting_all_but_one_string_still_passes(self):
        self.commit_base(app=po(entry("about", "About"), entry("bible", "Bible")))
        self.write("app.po", po(entry("about", "About")))
        self.commit("keep one")
        self.assertIn("1 deleted", self.assertPasses(self.run_validator()))

    def test_touching_an_already_empty_file_is_not_flagged(self):
        # licenses.po ships empty; adding to it must not look like a removal.
        self.commit_base(app=po(entry("about", "About")), licenses=HEADER)
        self.write("licenses.po", po(entry("mitLicense", "MIT License")))
        self.commit("populate empty file")
        stdout = self.assertPasses(self.run_validator())
        self.assertIn("1 added", stdout)
        self.assertNotIn("removed", stdout)

    def test_empty_value_fails(self):
        self.commit_base(app=po(entry("about", "About")))
        self.write("app.po", po(entry("about", "About"), entry("bible", "")))
        self.commit("empty")
        self.assertIn("empty value", self.assertFails(self.run_validator()))

    def test_notices_are_still_printed_alongside_an_error(self):
        self.commit_base(app=po(entry("about", "About"), entry("bible", "Bible")))
        self.write("app.po", po(entry("about", "About Us"), entry("bible", "Bible"), entry("bible", "Bible")))
        self.commit("mixed")
        stdout = self.assertFails(self.run_validator())
        self.assertIn("1 edited", stdout)
        self.assertIn("defined more than once", stdout)


if __name__ == "__main__":
    unittest.main()
