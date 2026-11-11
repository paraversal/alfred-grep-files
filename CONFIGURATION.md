# Configuration reference

Grep Files has one user-facing configuration surface: Alfred's **Configure Workflow…** window. Every field there is declared in `info.plist` under `userconfigurationconfig`, and Alfred exports the selected values to the workflow scripts as environment variables.

This document covers:

- the settings shown in the config window, with defaults and examples;
- how defaults, overrides, and environment variables fit together;
- the exact plist shape, for anyone editing `info.plist` or adding a field;
- what is deliberately **not** configurable.

## Where configuration lives

| Layer | File / place | Purpose |
| --- | --- | --- |
| Shipped defaults | `info.plist` → `userconfigurationconfig` | The values Alfred shows before the user changes anything. |
| User overrides | `prefs.plist` in the workflow folder | Written by Alfred when the user edits **Configure Workflow…**. Gitignored; do not ship it. |
| Runtime values | Environment variables | Alfred exports each field's `variable` and the user's value to the Script Filter and action scripts. |
| Code fallbacks | `search.py` / `action.py` | Defaults used when a variable is absent, e.g. when running a script directly. They mirror the plist defaults. |

The Script Filter's keyword is dynamic:

```text
{var:grep_keyword}
```

Changing the **Keyword** field therefore changes the Alfred trigger itself; there is no hardcoded `f` in the Script Filter. The workflow also sets one non-user variable, `PATH`, so Alfred can find `rg`, `fzf`, and `python3`; it is not shown in the config window.

To **reset** everything, remove `prefs.plist` from the workflow folder (or re-import the `.alfredworkflow`). Alfred then falls back to the defaults in `info.plist`.

## Settings shown in Alfred

Open the workflow → **Configure Workflow…**. The field labels match the ones Alfred displays; each field's in-app description is the `description` string in `info.plist`, expanded below. The workflow's `readme` (also in `info.plist`) repeats the field list and points back to this document.

| Field | Environment variable | Type | Default | Accepted values |
| --- | --- | --- | --- | --- |
| Keyword | `grep_keyword` | text | `f` | One word |
| Search folder | `search_root` | text | `~` | Existing folder; `~` and env vars expand |
| Excluded folders | `excludes_mode` | popup | `asimeow` | `asimeow`, `builtin`, `none` |
| asimeow config path | `asimeow_config` | text | `~/.config/asimeow/config.yaml` | Path to a YAML file |
| Extra excluded folders | `extra_excludes` | text area | empty | Names, path patterns, suffixes, or globs |
| File contents | `content_enabled` | checkbox | on | on / off |
| Maximum results | `max_items` | text | `30` | Integer 1–100 |
| Content hits per file | `max_content_lines` | text | `3` | Integer 1–10 |
| Folders | `include_dirs` | checkbox | on | on / off |
| Editor command | `editor_command` | text | empty | Shell-like command with `{path}`, `{line}`, `{col}` |
| Index refresh interval | `index_ttl_seconds` | text | `600` | Integer 0–86400 seconds |

### Keyword

- **Variable:** `grep_keyword` · **Default:** `f` · **Required, trimmed**
- The word you type in Alfred before the query. Use a single word; the first space starts the query.
- Changes the Script Filter keyword via `{var:grep_keyword}`. No restart or workflow re-import is needed after saving.
- If you leave it empty, Alfred's required-field validation should stop you; the code has no separate keyword fallback because the trigger is resolved by Alfred, not `search.py`.

### Search folder

- **Variable:** `search_root` · **Default:** `~`
- The root that is indexed and searched recursively. `~` and environment variables such as `$HOME` are expanded, then the path is realpath'd.
- Hidden files and folders are included (apart from exclusions). Symlinks are not followed.
- The folder must exist. A missing or unreadable root produces an Alfred error item rather than a blank panel.
- Spaces are fine: `/Users/you/My Files` works.
- Changing the root invalidates the index and starts a rebuild on the next search.

### Excluded folders

- **Variable:** `excludes_mode` · **Default:** `asimeow` · **Popup**
- `asimeow` — read the config at **asimeow config path** and flatten `ignore[]` plus every `rules[*].exclusions[]` into global directory names.
- `builtin` — use the workflow's built-in dependency-folder list (`node_modules`, `target`, `.venv`, `Pods`, …).
- `none` — use only the workflow baseline.

The baseline is always excluded regardless of mode: `.git`, `.svn`, `.hg`, `.DS_Store`, and the workflow's own cache directory.

asimeow's per-project marker semantics are flattened: a name is excluded globally, not only in projects where asimeow's `file_match` marker exists. A missing or unreadable asimeow config falls back to the built-in list.

### asimeow config path

- **Variable:** `asimeow_config` · **Default:** `~/.config/asimeow/config.yaml`
- Used only when **Excluded folders** is `asimeow`. `~` is expanded.
- The config is read at invocation time, with an mtime-based cache. The parser intentionally understands only the subset it needs:
  - top-level `ignore:` list items;
  - `rules:` entries and their `exclusions:` list items.
- Unknown keys are ignored. If the file is missing or cannot be parsed, the built-in list is used.

### Extra excluded folders

- **Variable:** `extra_excludes` · **Default:** empty · **Text area, trimmed**
- One entry per line; commas also separate entries. For a name containing a comma, use a newline instead.
- Entries are applied on top of the selected exclusion mode and the baseline.

Supported entry forms:

| Form | Example | What it matches |
| --- | --- | --- |
| Bare name | `build` | Any path component named `build`, at any depth |
| Path pattern | `Media/Audio Files` | That path suffix at any depth, e.g. `Project.logicx/Media/Audio Files/…` |
| Dot suffix | `.logicproject` | Any `*.logicproject` package, including `Song.logicproject`; add `.logicx` for current Logic projects if needed |
| Glob suffix | `*.tmp` | Any path ending in `.tmp` |
| Full glob | `Caches/**` | ripgrep `-g` syntax; matched at any depth |

Rules:

- Path patterns are relative, not absolute. A leading `/` is ignored; absolute paths are not supported because all searches are already rooted at **Search folder**.
- Path and glob entries are internally anchored with `**/` so they work anywhere under the root, regardless of the process's working directory.
- All exclusions are applied by ripgrep when building the index and when scanning contents. fzf only ranks the already-filtered path list.
- Changing this field invalidates the index signature and triggers a rebuild on the next search.
- ripgrep's own `.gitignore`, `.ignore`, and `.rgignore` handling still applies on top.

### File contents

- **Variable:** `content_enabled` · **Default:** on · **Checkbox**
- On: names paint first, then a background ripgrep scan appends content matches below them.
- Off: filename/folder search only; no background content scan is started.
- The content stage is literal (fixed-string) matching. Multiple words mean all words on the same line, in any order.
- Content results are deferred and cached for identical root + query for 2 minutes; the scan has a 3-second budget.

### Maximum results

- **Variable:** `max_items` · **Default:** `30` · **Integer 1–100**
- Total number of items emitted to Alfred. Name hits are always above content hits.
- The pipeline still enforces its own caps: at most 20 name hits and 10 content items. Lowering `max_items` reduces the total; it does not raise those caps.
- When more results exist than are shown, the last row becomes a non-actionable “Showing top N / Type more to narrow” note.
- Alfred's result window shows about 9 rows at a time and scrolls for the rest.

### Content hits per file

- **Variable:** `max_content_lines` · **Default:** `3` · **Integer 1–10**
- Maximum matching lines taken from a single file before moving on.
- ripgrep is capped per file with `--max-count`; files larger than 5 MB are skipped.

### Folders

- **Variable:** `include_dirs` · **Default:** on · **Checkbox**
- Include matching folders alongside files. Folders are shown as `📁 name/` and open in Finder (reveal) rather than in an application.
- Folder paths are derived from the parents of indexed files, so non-empty folders are covered without a second filesystem walk. Empty folders are the known gap and do not appear.

### Editor command

- **Variable:** `editor_command` · **Default:** empty
- Used for **Enter** on a content hit, to open the file at the matched line. Placeholders:
  - `{path}` — absolute file path
  - `{line}` — 1-based line number
  - `{col}` — 1-based column number
- The command is split with shell-like quoting, then placeholders are replaced inside each token, so paths with spaces survive.

Examples:

```text
code -g {path}:{line}:{col}
cursor -g {path}:{line}:{col}
zed {path}:{line}:{col}
subl {path}:{line}:{col}
nvim +{line} {path}
```

Leave empty to auto-detect the first installed editor in this order: `code`, `cursor`, `zed`, `subl`; if none is found, fall back to `open`.

If the configured command cannot be found, the action falls back to `open` rather than failing silently.

### Index refresh interval

- **Variable:** `index_ttl_seconds` · **Default:** `600` · **Integer 0–86400 seconds**
- How old the filename index may get before a background rebuild is started on the next search. `0` means rebuild on every search.
- The existing index is served while the rebuild runs (stale-while-revalidate), so a refresh does not block results.
- Changing **Search folder** or **Extra excluded folders** invalidates the index immediately, independent of this interval.
- The content cache is separate and fixed at 2 minutes; it is not controlled by this field.

## Plist shape (for maintainers)

`info.plist` contains a top-level `userconfigurationconfig` array. Each entry describes one field in the Configure Workflow window:

```json
{
  "type": "textfield",
  "variable": "search_root",
  "label": "Search folder",
  "description": "Root folder to search recursively. ~ expands to your home folder. Default: ~.",
  "config": {
    "default": "~",
    "placeholder": "~",
    "required": true,
    "trim": true
  }
}
```

The field keys are:

| Key | Meaning |
| --- | --- |
| `type` | `textfield`, `textarea`, `checkbox`, or `popupbutton` (types used here) |
| `variable` | Environment variable name exported by Alfred; must match what the scripts read |
| `label` | Field title shown in Configure Workflow |
| `description` | Help text shown under the field in Configure Workflow |
| `config` | Type-specific defaults and constraints |

The `config` keys used in this workflow:

```text
textfield:   default, placeholder, required, trim
textarea:    default, required, trim, verticalsize
checkbox:    default (bool), required, text
popupbutton: default, pairs: [[label, value], ...]
```

Field order in the array is the order shown in Alfred. Current fields, in order:

```text
grep_keyword
search_root
excludes_mode
asimeow_config
extra_excludes
content_enabled
max_items
max_content_lines
include_dirs
editor_command
index_ttl_seconds
```

### Adding or changing a field

1. Edit `info.plist` and add/update the `userconfigurationconfig` entry. Keep the `variable` lowercase and stable.
2. Read it in the relevant script. `search.py` uses:
   - `_env("name", default)` for strings;
   - `_bool_env("name", default)` for booleans;
   - `_int_env("name", default, minimum, maximum)` for clamped integers.
3. Mirror the plist default in the code fallback so direct runs and tests behave the same.
4. Update the `test.sh` configuration contract (expected variable set, defaults, popup pairs, dynamic keyword), and this document.
5. Run `./test.sh`, `plutil -lint info.plist`, rebuild with `./build.sh`, and re-import or reload the workflow.

`prefs.plist` is user state, not a source file. Never commit it and never use it to change shipped defaults. The top-level `variables` dictionary is also not a user-settings surface; it currently contains only `PATH` because Alfred's script environment is minimal. Putting user-facing defaults there as well as in `userconfigurationconfig` creates confusing precedence.

The CI workflow checks that `info.plist` parses, that `search.py` and `action.py` compile, and that the archive contains `icon.png` and `CONFIGURATION.md`.

## Precedence and validation

1. `prefs.plist` override (if the user has saved one)
2. `info.plist` default
3. Code fallback in `search.py` / `action.py`

Number fields are clamped by the code even if Alfred lets an out-of-range value through: `max_items` to 1–100, `max_content_lines` to 1–10, `index_ttl_seconds` to 0–86400. An unparseable number falls back to the default.

Boolean fields are exported by Alfred as `1`/`0`; `_bool_env` also accepts `true`, `yes`, and `on` so direct shell testing is forgiving.

`excludes_mode` is validated in code: an unknown value falls back to `asimeow`.

## Not configurable

These are fixed in the scripts for predictability:

- fzf's `--scheme=path` scoring and the 20-name-hit cap.
- The 10-content-item cap, 5 MB per-file limit, 4 MiB scan-output cap, 10k parsed-hit cap, 3-second scan budget, and 2-minute content cache.
- The exclusion baseline (`.git`, `.svn`, `.hg`, `.DS_Store`, workflow cache).
- The action shortcuts (Enter, Option+Enter, Command+Enter, Tab) and the editor auto-detect order; `editor_command` overrides the command but not the shortcuts.
- Hidden-file inclusion and the “do not follow symlinks” behavior.
- `PATH` used to find `rg`, `fzf`, and `python3`.

## Troubleshooting

- **The config window does not show a field.** Check that the entry is inside `userconfigurationconfig` in `info.plist`, the variable name is unique, and the workflow has been re-imported/reloaded. `plutil -lint info.plist` should say OK.
- **The keyword does nothing.** Confirm the field has a single word, the workflow is enabled, and the Script Filter keyword is `{var:grep_keyword}`. Saving the configuration should update it.
- **A new exclusion has no effect.** Check for typos and remember that path patterns are relative and matched at any depth. Changing the field should make the index stale; if not, force a rebuild with `search.py --rebuild`.
- **Settings seem ignored.** An old `prefs.plist` may be supplying values. Remove it to reset to `info.plist` defaults, or check the values in the Alfred debugger.
- **Missing asimeow config.** The workflow silently falls back to the built-in exclusion list; the empty-query hint shows which source is active.
- **Editor does not open at the line.** Check `editor_command`; an unknown executable falls back to `open`. Test the command in Terminal first.
