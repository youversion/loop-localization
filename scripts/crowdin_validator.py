#!/usr/bin/env python3
"""CI gate for hand-edited Crowdin source strings.

Counterpart to `scripts/crowdin_sync.py` (which pulls source strings and
pushes local ones). Adds, edits and deletes are all made by hand-editing the
target `.po` file directly under `strings/en/` -- no script needed for that
part, and no Crowdin UI round-trip required.

This script diffs every `.po` file changed in the current branch, vs. the
base branch, and splits what it finds in two:

  NOTE (reported, does not fail the build)
      Added, edited, deleted and renamed strings. These are all legitimate
      deltas; the notices exist so a reviewer reading the CI log can see
      exactly which existing strings a PR touched, and what it costs on the
      Crowdin side (an edit unapproves that string's translations; a rename
      loses them outright).

  ERROR (fails the build)
      A key defined twice in one file, an entry this script cannot parse, or
      an entry with an empty value -- each of these is either ambiguous or
      invisible to the diff above, so it has to stop the build. Plus a new
      key that duplicates one in a different file: Crowdin scopes keys per
      file and tolerates that (53 such pairs already exist on main), but it
      is almost always a copy-paste slip, so it is worth a deliberate second
      look rather than a silent merge.

      Wiping out a whole file is an error too -- see removed_file() below.
      Deleting strings is fine; deleting the file that holds them is not.

Structural checks run only over the files the PR actually changed, so a
pre-existing quirk elsewhere can never fail an unrelated PR.

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
from collections import Counter
from pathlib import Path

EN_STRINGS_DIR = Path("strings/en")
BASE_BRANCH = os.environ.get("CROWDIN_SYNC_BASE", "main")

# Must match crowdin_sync.py's BRANCH default/env var exactly: PRs from this
# rolling branch are crowdin_sync.py's own pull-mode output, mirroring
# whatever Crowdin's UI/API currently has. It is generated, never reviewed as
# a hand-edit, so none of the checks below tell us anything useful about it.
SYNC_BRANCH = os.environ.get("CROWDIN_SYNC_BRANCH", "chore/crowdin-sync")

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

# Pulls the value out of each msgstr line of an already-matched entry body.
_MSGSTR_VALUE_RE = re.compile(r'^msgstr(?:\[\d+\])? "((?:[^"\\]|\\.)*)"$', re.MULTILINE)

# A bare quoted line -- PO's multi-line continuation syntax. _ENTRY_RE stops
# at the first msgstr line, so an entry continued this way parses as an entry
# with an empty value and the continuation is silently dropped; matched right
# after an entry body, this is how we catch that.
_CONTINUATION_RE = re.compile(r'"(?:[^"\\]|\\.)*"[ \t]*(?:\r?\n|$)')

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
        for key in all_keys(content):
            keys.setdefault(key, set()).add(po_path.name)
    return keys


def all_keys(content: str) -> list[str]:
    """Every non-header msgid in the file, in order, including duplicates."""
    keys = [po_unescape(match.group(1)) for match in _ANY_MSGID_RE.finditer(content)]
    return [key for key in keys if key]


def parse_entries(content: str) -> dict[str, str]:
    return {
        po_unescape(match.group(1)): match.group(2)
        for match in _ENTRY_RE.finditer(content)
        if match.group(1)
    }


def duplicate_keys(content: str) -> list[tuple[str, int]]:
    """Keys defined more than once in a single file.

    parse_entries() silently keeps only the last definition, so without this
    check a duplicate would make one of the two entries invisible to the
    add/edit/delete diff below -- and ambiguous on the Crowdin side.
    """
    counts = Counter(all_keys(content))
    return sorted((key, count) for key, count in counts.items() if count > 1)


def malformed_keys(content: str) -> list[str]:
    """Keys this script cannot faithfully round-trip.

    Catches hand-edits that put a `#.`/`#:` comment between msgid and msgstr
    or dropped the msgstr entirely (invisible to _ENTRY_RE), and ones that
    used PO multi-line continuation (parsed, but with the continuation lines
    silently discarded). Either way the entry's real value would be invisible
    to the diff, so it has to fail rather than pass -- the canonical
    single-line shape is what update_strings.py always writes.
    """
    parsed = parse_entries(content)
    bad = {key for key in all_keys(content) if key not in parsed}
    for match in _ENTRY_RE.finditer(content):
        key = po_unescape(match.group(1))
        if key and _CONTINUATION_RE.match(content, match.end()):
            bad.add(key)
    return sorted(bad)


def empty_value_keys(entries: dict[str, str]) -> list[str]:
    """Keys whose every msgstr value is empty -- a key with no English text."""
    return sorted(
        key
        for key, body in entries.items()
        if not any(_MSGSTR_VALUE_RE.findall(body))
    )


def removed_file(path: Path, base: dict[str, str], current: dict[str, str]) -> str | None:
    """Describe a `.po` that this PR wipes out entirely, or None.

    Deleting individual strings is supported; deleting the file holding them
    is not, and the two are worth separating. `crowdin upload sources` can
    only ever *upload* a file, so a removed file is invisible to the push:
    it stays in the Crowdin project and the next pull restores it, along with
    every string in it. Emptying a file in place is the same outcome by
    another route, so it is caught here too -- but only for a file that had
    entries to begin with, since licenses.po is legitimately empty already.
    """
    if not base or current:
        return None
    state = "deleted" if not path.exists() else "emptied"
    return f"{path} ({state}, {len(base)} string(s))"


def entry_text(body: str) -> str:
    """Human-readable rendering of an entry body, for the CI log."""
    return " / ".join(po_unescape(value) for value in _MSGSTR_VALUE_RE.findall(body))


def detect_renames(
    deleted: dict[str, str], added: dict[str, str]
) -> list[tuple[str, str, str]]:
    """Pair deleted keys with added keys carrying identical text.

    A rename is the one delta where existing translations do not survive:
    Crowdin matches strings by identifier, so the new key arrives untranslated
    while the old key's translations are discarded with it. Worth calling out
    separately instead of burying it in the delete list.
    """
    unclaimed = dict(added)
    renames = []
    for old_key, body in sorted(deleted.items()):
        for new_key, new_body in sorted(unclaimed.items()):
            if new_body == body:
                renames.append((old_key, new_key, body))
                del unclaimed[new_key]
                break
    return renames


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


def report(label: str, items: list[str]) -> None:
    if not items:
        return
    print(label)
    for item in items:
        print(f"  - {item}")


def validate() -> int:
    changed = changed_po_files()
    if not changed:
        print(f"OK: no strings changed under {EN_STRINGS_DIR}/.")
        return 0

    existing = find_existing_keys(EN_STRINGS_DIR)
    added, edited, deleted, renamed = [], [], [], []
    collisions, duplicates, malformed, empties, removed = [], [], [], [], []

    for path in changed:
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        base = parse_entries(base_content(path))
        current = parse_entries(content)

        wiped = removed_file(path, base, current)
        if wiped:
            # Report the file once rather than every string it held.
            removed.append(wiped)
            continue

        unparseable = malformed_keys(content)
        for key, count in duplicate_keys(content):
            duplicates.append(f"{key} ({path}, defined {count}x)")
        for key in unparseable:
            malformed.append(f"{key} ({path})")
        # A continued entry also looks empty; report it once, as unparseable.
        for key in empty_value_keys(current):
            if key not in unparseable:
                empties.append(f"{key} ({path})")

        for key in current:
            if key in base:
                continue
            other_files = existing.get(key, set()) - {path.name}
            if other_files:
                collisions.append(f"{key} ({path}, also in {', '.join(sorted(other_files))})")

        gone = {key: body for key, body in base.items() if key not in current}
        new = {key: body for key, body in current.items() if key not in base}
        renames = detect_renames(gone, new)

        for old_key, new_key, body in renames:
            renamed.append(f'{old_key} -> {new_key} ({path}): "{entry_text(body)}"')
            del gone[old_key]
            del new[new_key]

        for key in sorted(new):
            added.append(f'{key} ({path}): "{entry_text(new[key])}"')
        for key in sorted(gone):
            deleted.append(f'{key} ({path}): "{entry_text(gone[key])}"')
        for key in sorted(base):
            if key in current and current[key] != base[key]:
                edited.append(
                    f'{key} ({path}): "{entry_text(base[key])}" -> "{entry_text(current[key])}"'
                )

    summary = (
        f"{len(changed)} file(s) changed: {len(added)} added, {len(edited)} edited, "
        f"{len(deleted)} deleted, {len(renamed)} renamed."
    )
    if removed:
        summary += f" {len(removed)} file(s) removed."
    print(summary)

    report("NOTE: string(s) added:", added)
    report(
        "NOTE: existing string(s) edited -- Crowdin keeps the existing "
        "translations but marks them unapproved for re-review:",
        edited,
    )
    report(
        "NOTE: existing string(s) deleted -- removed from Crowdin by the next "
        "`crowdin_sync.py --push`:",
        deleted,
    )
    report(
        "NOTE: likely rename(s) -- Crowdin matches by key, so the existing "
        "translations do NOT carry over to the new key:",
        renamed,
    )

    if not (removed or collisions or duplicates or malformed or empties):
        return 0

    report(
        "ERROR: whole file(s) removed -- delete the individual strings instead "
        "and keep the file, or remove the file in the Crowdin UI first "
        "(`crowdin upload sources` can only upload, so a removed file survives "
        "in Crowdin and comes back on the next pull):",
        removed,
    )
    report("ERROR: new string(s) duplicate an existing key in another file:", collisions)
    report("ERROR: key(s) defined more than once in the same file:", duplicates)
    report(
        "ERROR: unparseable entry -- msgstr must be a single escaped line "
        "immediately after msgid (see update_strings.py's render_entries):",
        malformed,
    )
    report("ERROR: entry with an empty value:", empties)
    return 1


def head_branch() -> str:
    # Bitrise's git-clone step checks out PRs as a detached merge ref, so
    # `git branch --show-current` is empty -- the actual PR head branch name
    # is only available via the env vars CI exports. Bitrise sets
    # BITRISE_GIT_BRANCH to the git-clone step's resolved `branch` input
    # (the PR's head branch); GitHub Actions would set GITHUB_HEAD_REF. Fall
    # back to the real current branch for local/manual runs.
    return (
        os.environ.get("BITRISE_GIT_BRANCH")
        or os.environ.get("GITHUB_HEAD_REF")
        or capture(["git", "branch", "--show-current"])
    )


def main() -> int:
    root = capture(["git", "rev-parse", "--show-toplevel"])
    os.chdir(root)

    if head_branch() == SYNC_BRANCH:
        print(f"OK: skipping validation for crowdin_sync.py's own '{SYNC_BRANCH}' branch.")
        return 0

    # A PR checkout may not already have origin/{BASE_BRANCH} fetched locally
    # (depends on the CI clone depth) -- fetch it explicitly so the diff
    # below can't fail just because the ref is missing.
    capture(["git", "fetch", "origin", BASE_BRANCH])
    return validate()


if __name__ == "__main__":
    sys.exit(main())
