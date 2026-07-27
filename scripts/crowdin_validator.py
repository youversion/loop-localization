#!/usr/bin/env python3
"""CI gate: fail a PR if it edits or deletes an existing Crowdin string.

Counterpart to `scripts/crowdin_sync.py` (which pulls translations and pushes
new source strings). Crowdin remains the single source of truth: existing
strings can only be edited or deleted via the Crowdin UI. New strings are
added by hand-editing the target `.po` file directly under `strings/en/` --
no script needed for that part.

This script fails if any `.po` file changed in the current branch, vs. the
base branch, edited or deleted an existing entry, or introduced a "new" key
that collides with an existing key in a different, unchanged file. New,
non-colliding entries are the expected delta.

Pure git + local `.po` parsing -- never touches the Crowdin API or needs
`CROWDIN_API_TOKEN`. Intended to run in Bitrise on PRs touching
strings/en/*.po.

Ported from youversion-flutter-loop/scripts/crowdin_validator.py (see BL-1870).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

EN_STRINGS_DIR = Path("strings/en")
BASE_BRANCH = os.environ.get("CROWDIN_SYNC_BASE", "main")

# Matches any `msgid "..."` line (including ones followed by msgid_plural) --
# used only to detect whether a key exists anywhere, not to read its value.
_ANY_MSGID_RE = re.compile(r'^msgid "((?:[^"\\]|\\.)*)"$', re.MULTILINE)

# Matches a msgid and its full definition body: either a plain msgstr line,
# or msgid_plural followed by one or more msgstr[N] lines. The body is
# captured as opaque text for equality comparison -- this doesn't need to
# understand plural forms, just detect whether they changed.
_ENTRY_RE = re.compile(
    r'^msgid "((?:[^"\\]|\\.)*)"\r?\n'
    r'((?:msgid_plural "(?:[^"\\]|\\.)*"\r?\n)?'
    r'(?:msgstr(?:\[\d+\])? "(?:[^"\\]|\\.)*"\r?\n?)+)',
    re.MULTILINE,
)

_ESCAPE_RE = re.compile(r"\\(.)")
_UNESCAPE_MAP = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def capture(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, text=True, capture_output=True).stdout.strip()


def po_unescape(value: str) -> str:
    return _ESCAPE_RE.sub(lambda m: _UNESCAPE_MAP.get(m.group(1), m.group(1)), value)


def find_existing_keys(strings_dir: Path) -> dict[str, set[str]]:
    """Return {key: {filenames the key appears in}} across all .po files."""
    keys: dict[str, set[str]] = {}
    for po_path in sorted(strings_dir.glob("*.po")):
        content = po_path.read_text(encoding="utf-8")
        for match in _ANY_MSGID_RE.finditer(content):
            key = po_unescape(match.group(1))
            if key:
                keys.setdefault(key, set()).add(po_path.name)
    return keys


def parse_entries(content: str) -> dict[str, str]:
    return {
        po_unescape(match.group(1)): match.group(2)
        for match in _ENTRY_RE.finditer(content)
        if match.group(1)
    }


def changed_po_files() -> list[Path]:
    output = capture(
        ["git", "diff", "--name-only", f"origin/{BASE_BRANCH}...HEAD", "--", str(EN_STRINGS_DIR)]
    )
    return [Path(line) for line in output.splitlines() if line.endswith(".po")]


def base_content(path: Path) -> str:
    result = subprocess.run(
        ["git", "show", f"origin/{BASE_BRANCH}:{path.as_posix()}"],
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def validate() -> int:
    changed = changed_po_files()
    if not changed:
        print(f"OK: no strings changed under {EN_STRINGS_DIR}/.")
        return 0

    existing = find_existing_keys(EN_STRINGS_DIR)
    edited, deleted, collisions = [], [], []

    for path in changed:
        base = parse_entries(base_content(path))
        current = parse_entries(path.read_text(encoding="utf-8")) if path.exists() else {}

        for key, base_text in base.items():
            if key not in current:
                deleted.append(f"{key} ({path})")
            elif current[key] != base_text:
                edited.append(f"{key} ({path})")

        for key in current:
            if key in base:
                continue
            other_files = existing.get(key, set()) - {path.name}
            if other_files:
                collisions.append(f"{key} ({path}, also in {', '.join(sorted(other_files))})")

    if not edited and not deleted and not collisions:
        print("OK: only new, non-colliding strings added.")
        return 0

    if edited:
        print(f"ERROR: existing string(s) edited outside the Crowdin UI: {', '.join(edited)}")
    if deleted:
        print(f"ERROR: existing string(s) deleted outside the Crowdin UI: {', '.join(deleted)}")
    if collisions:
        print(f"ERROR: new string(s) collide with an existing key elsewhere: {', '.join(collisions)}")
    print("Existing strings can only be changed via the Crowdin UI.")
    return 1


def main() -> int:
    root = capture(["git", "rev-parse", "--show-toplevel"])
    os.chdir(root)
    # A PR checkout may not already have origin/{BASE_BRANCH} fetched locally
    # (depends on the CI clone depth) -- fetch it explicitly so the diff
    # below can't fail just because the ref is missing.
    capture(["git", "fetch", "origin", BASE_BRANCH])
    return validate()


if __name__ == "__main__":
    sys.exit(main())
