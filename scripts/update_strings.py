#!/usr/bin/env python3
"""Download English .po files from Crowdin into strings/en/.

Run from the repository root::

    python3 scripts/update_strings.py

This re-downloads strings/en/*.po as currently stored on Crowdin -- what
self-heals the repo after a Crowdin-UI-side edit or deletion. There is no
`download sources`/`file download` shortcut for a source-only pull in
crowdin-cli 4.12.0 -- `crowdin download`/`file download` only export
*translations*.

Uses Crowdin bundle 13 (the same bundle youversion-flutter-loop's own pull
pipeline uses) -- confirmed via a live download that it's scoped to exactly
"Bible Loop (Master)/*.po" (all 30 files, matching this repo's strings/en/,
minus licenses.po which isn't Crowdin-sourced) and includes the en-US
(source language) XLIFF export alongside translations. Unlike
youversion-flutter-loop, this script parses the XLIFF directly with the
stdlib (no vendored `babel` binary) since this repo is platform-agnostic and
only needs the source language back out, not generated Dart.

XLIFF plural entries are wrapped in <group restype="x-gettext-plurals"> with
one <trans-unit id="NNNN[i]" resname="key"> per plural form, indexed by the
bracketed suffix on `id` (0 = "one", 1 = "other" for English). Regular
entries are plain <trans-unit resname="key"><source>...</source></trans-unit>
outside any group.

Requires ``crowdin`` CLI and ``CROWDIN_API_TOKEN`` (see crowdin/crowdin.yml).
Override the bundle via env var CROWDIN_BUNDLE_ID if 13 ever changes.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CROWDIN_DIR = Path("crowdin")
BUNDLE_DIR = CROWDIN_DIR / "bundle"
EN_STRINGS_DIR = Path("strings/en")
DEFAULT_BUNDLE_ID = "13"

XLIFF_NS = "{urn:oasis:names:tc:xliff:document:1.2}"
_PLURAL_INDEX_RE = re.compile(r"\[(\d+)\]$")


def repo_root() -> Path:
    return Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, text=True, cwd=cwd)


def download_bundle(root: Path, bundle_id: str) -> Path:
    bundle_dir = root / BUNDLE_DIR
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)

    print(f"Downloading Crowdin bundle {bundle_id} (project 257)...")
    run(
        ["crowdin", "bundle", "download", bundle_id, "--base-path", "bundle"],
        cwd=root / CROWDIN_DIR,
    )
    return bundle_dir


def find_source_xliff(bundle_dir: Path) -> Path:
    candidates = sorted(bundle_dir.glob("en-US.xliff")) or sorted(bundle_dir.glob("en.xliff"))
    if not candidates:
        raise FileNotFoundError(
            f"No en-US.xliff/en.xliff found under {bundle_dir}. "
            "Confirm the bundle includes the source language."
        )
    return candidates[0]


def po_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def element_text(el: ET.Element | None) -> str:
    return el.text or "" if el is not None else ""


def parse_xliff_files(xliff_path: Path) -> dict[str, list[tuple[str, list[str]]]]:
    """Return {po_filename: [(key, [form values...]), ...]}, order preserved."""
    root = ET.parse(xliff_path).getroot()
    files: dict[str, list[tuple[str, list[str]]]] = {}

    for file_el in root.findall(f"{XLIFF_NS}file"):
        filename = Path(file_el.get("original", "")).name
        body = file_el.find(f"{XLIFF_NS}body")
        if not filename or body is None:
            continue

        entries: list[tuple[str, list[str]]] = []
        for child in body:
            tag = child.tag.removeprefix(XLIFF_NS)
            if tag == "trans-unit":
                key = child.get("resname", "")
                value = element_text(child.find(f"{XLIFF_NS}source"))
                entries.append((key, [value]))
            elif tag == "group" and child.get("restype") == "x-gettext-plurals":
                key = ""
                forms: dict[int, str] = {}
                for i, tu in enumerate(child.findall(f"{XLIFF_NS}trans-unit")):
                    key = tu.get("resname", "")
                    match = _PLURAL_INDEX_RE.search(tu.get("id", ""))
                    idx = int(match.group(1)) if match else i
                    forms[idx] = element_text(tu.find(f"{XLIFF_NS}source"))
                entries.append((key, [forms[i] for i in sorted(forms)]))

        files[filename] = entries

    return files


def render_entries(entries: list[tuple[str, list[str]]]) -> str:
    blocks = []
    for key, values in entries:
        if not key:
            continue
        if len(values) == 1:
            blocks.append(f'msgid "{po_escape(key)}"\nmsgstr "{po_escape(values[0])}"')
        else:
            lines = [f'msgid "{po_escape(key)}"', f'msgid_plural "{po_escape(key)}"']
            lines += [f'msgstr[{i}] "{po_escape(v)}"' for i, v in enumerate(values)]
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def existing_header(path: Path) -> str:
    """Preserve the current file's po header (metadata block) if present."""
    if not path.exists():
        return 'msgid ""\nmsgstr ""\n"Language: en-US\\n"\n'
    text = path.read_text(encoding="utf-8")
    header, _, _ = text.partition("\n\n")
    return header + "\n"


def write_po_file(destination: Path, entries: list[tuple[str, list[str]]]) -> None:
    header = existing_header(destination)
    destination.write_text(f"{header}\n{render_entries(entries)}\n", encoding="utf-8")
    print(f"Wrote {destination}")


def update_strings() -> None:
    bundle_id = os.environ.get("CROWDIN_BUNDLE_ID", DEFAULT_BUNDLE_ID)
    root = repo_root()

    bundle_dir = download_bundle(root, bundle_id)
    xliff_path = find_source_xliff(bundle_dir)
    files = parse_xliff_files(xliff_path)

    destination_dir = root / EN_STRINGS_DIR
    destination_dir.mkdir(parents=True, exist_ok=True)
    for filename, entries in files.items():
        write_po_file(destination_dir / filename, entries)


def main() -> int:
    try:
        update_strings()
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
