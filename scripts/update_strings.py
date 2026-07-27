#!/usr/bin/env python3
"""Download English .po files from Crowdin into strings/en/.

Run from the repository root::

    python3 scripts/update_strings.py

This re-downloads strings/en/*.po as currently stored on Crowdin -- what
self-heals the repo after a Crowdin-UI-side edit or deletion. There is no
`download sources`/`file download` shortcut for a source-only pull in
crowdin-cli 4.12.0 -- `crowdin download`/`file download` only export
*translations*. Bundle download is the only verified mechanism (same
approach loop-ios's scripts/update_strings.py and
youversion-flutter-loop's packages/localization/crowdin/update-strings use
for their own pulls), so this requires a Crowdin bundle already scoped to
these files and configured with "include source language", identified by
env var CROWDIN_BUNDLE_ID.

Requires ``crowdin`` CLI and ``CROWDIN_API_TOKEN`` (see crowdin/crowdin.yml).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

CROWDIN_DIR = Path("crowdin")
BUNDLE_DIR = CROWDIN_DIR / "bundle"
EN_STRINGS_DIR = Path("strings/en")


def repo_root() -> Path:
    return Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"ERROR: required environment variable {name} is unset or empty")
    return value


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


def find_en_dir(bundle_dir: Path) -> Path:
    en_dirs = [d for d in bundle_dir.rglob("*") if d.is_dir() and d.name in ("en", "en-US")]
    if not en_dirs:
        raise FileNotFoundError(
            f"No en/en-US directory found under {bundle_dir}. "
            "Confirm CROWDIN_BUNDLE_ID points to a bundle that exports "
            "strings/en/*.po with source language included."
        )
    return en_dirs[0]


def update_strings() -> None:
    bundle_id = require_env("CROWDIN_BUNDLE_ID")
    root = repo_root()

    bundle_dir = download_bundle(root, bundle_id)
    en_dir = find_en_dir(bundle_dir)

    destination = root / EN_STRINGS_DIR
    destination.mkdir(parents=True, exist_ok=True)
    for po_file in en_dir.glob("*.po"):
        shutil.copy2(po_file, destination / po_file.name)
        print(f"Wrote {destination / po_file.name}")


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
