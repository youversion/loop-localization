# loop-localization

Platform-agnostic home for Bible Loop's English source strings. Tracked here
as the same per-domain `.po` files used by `loop-ios` and
`youversion-flutter-loop`, synchronized with the shared Crowdin project 257
via `scripts/crowdin_sync.py`.

- `strings/en/*.po` — the English source strings (`app.po`, `greetings.po`,
  `bible_loop.po`, ...).
- `crowdin/crowdin.yml` — Crowdin CLI config for project 257.
- `scripts/crowdin_sync.py` — CI entrypoint. Pulls current source strings
  from Crowdin (default mode, via `scripts/update_strings.py`) or pushes new
  local strings to Crowdin (`--push`). Runs on Bitrise; see the script's
  docstring for the three modes.
- `scripts/update_strings.py` — downloads a Crowdin bundle
  (`CROWDIN_BUNDLE_ID`) and installs its English `.po` files into
  `strings/en/`. Standalone/runnable on its own (`python3
  scripts/update_strings.py`) for local testing; requires a bundle scoped to
  these files with "include source language" enabled (there is no
  source-only download in crowdin-cli 4.12.0 — see the script's docstring).
- `scripts/crowdin_validator.py` — CI gate that fails a PR if it edits or
  deletes an existing string, or introduces a key that collides with one in
  another file. Needs no Crowdin credentials.

## Adding a new source string

Existing strings can only be edited or deleted via the Crowdin UI. To add a
brand-new one, edit the target `.po` file directly under `strings/en/`
(`app.po`, `felt_needs.po`, etc.) and append a new entry in the same style as
the rest of the file:

```
msgid "new_key"
msgstr "New English text"
```

Then commit and open a normal PR. `scripts/crowdin_validator.py` runs in CI
on that PR and fails if it edits or deletes an existing entry, or introduces
a key that collides with one already in another file — this needs no
Crowdin credentials. The next `scripts/crowdin_sync.py --push` run pushes the
new entries to Crowdin as an unconditional first step, ahead of its normal
pull.
