# loop-localization

Platform-agnostic home for Bible Loop's English source strings. Tracked here
as the same per-domain `.po` files used by `loop-ios` and
`youversion-flutter-loop`, synchronized with the shared Crowdin project 257
via `scripts/crowdin_sync.py`.

- `strings/en/*.po` — the English source strings (`app.po`, `greetings.po`,
  `bible_loop.po`, ...).
- `crowdin/crowdin.yml` — Crowdin CLI config for project 257.
- `scripts/crowdin_sync.py` — CI entrypoint. Pulls current source strings
  from Crowdin (default mode, via `scripts/update_strings.py`) or pushes
  local string changes to Crowdin (`--push`). Runs on Bitrise; see the script's
  docstring for the three modes.
- `scripts/update_strings.py` — downloads a Crowdin bundle
  (`CROWDIN_BUNDLE_ID`) and installs its English `.po` files into
  `strings/en/`. Standalone/runnable on its own (`python3
  scripts/update_strings.py`) for local testing; requires a bundle scoped to
  these files with "include source language" enabled (there is no
  source-only download in crowdin-cli 4.12.0 — see the script's docstring).
- `scripts/crowdin_validator.py` — CI gate on PRs touching `strings/en/*.po`.
  Reports every added, edited, deleted and renamed string, and fails only on
  a mistake: removing a whole `.po` file, a key that duplicates one in
  another file, a key defined twice in the same file, an unparseable entry,
  or an empty value. Needs no Crowdin credentials.

## Changing source strings

Add, edit and delete English strings by editing the target `.po` file
directly under `strings/en/` (`app.po`, `felt_needs.po`, etc.), in the same
style as the rest of the file:

```
msgid "new_key"
msgstr "New English text"
```

Then commit and open a normal PR. `scripts/crowdin_validator.py` runs in CI
on that PR (no Crowdin credentials needed) and prints what changed. It fails
the build only on a mistake — removing a whole `.po` file, a key that
duplicates one already in another file, the same key defined twice in one
file, an entry it cannot parse, or an entry with an empty value. The next
`scripts/crowdin_sync.py --push` run propagates all of it to Crowdin.

What each kind of change costs on the Crowdin side, since the validator
reports but does not block them:

| Change | Effect on existing translations |
| --- | --- |
| Add a `msgid` | None — the new string starts untranslated. |
| Edit a `msgstr` | Kept, but marked unapproved for re-review (`update_option` in `crowdin/crowdin.yml`). |
| Delete a `msgid` | Discarded along with the string. |
| Rename a `msgid` | **Lost.** Crowdin matches by key, so this is a delete plus an untranslated add. |

Deleting a whole `.po` file is rejected, whether by removing the file or by
emptying it in place. Delete the individual `msgid` entries and keep the
file. `crowdin upload sources` can only ever *upload* a file, so a file
removed here would survive in Crowdin and come back — along with all its
strings — on the next pull; retiring a whole file has to start in the
Crowdin UI. The non-English translations are likewise Crowdin's alone.
