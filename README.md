# Grep Files — Alfred workflow

Search a directory from Alfred in two passes:

1. **Names first** — fuzzy path matching with [fzf](https://github.com/junegunn/fzf), for files *and* folders.
2. **Contents second** — literal grep hits with [ripgrep](https://github.com/BurntSushi/ripgrep), always ranked below every name match.

It is built for large directories: the path list is indexed once and reused, name matches paint immediately, and content search runs in the background and appends its results when ready. Dependency and build trees such as `node_modules`, `target`, `.venv`, and `Pods` are skipped, so they do not slow the search down.

**[asimeow](https://github.com/mdnmdn/asimeow) support:** if an asimeow config exists at `~/.config/asimeow/config.yaml`, its exclusion rules are applied automatically; otherwise the workflow uses its built-in exclusion list. asimeow itself is optional.

**[Configuration reference →](CONFIGURATION.md)** — every field in Alfred's Configure Workflow window, with defaults, examples, and plist details.

## Requirements

- macOS + Alfred 5 with the Powerpack (Script Filters).
- `fzf` and `ripgrep`:

  ```bash
  brew install fzf ripgrep
  ```

- `python3` (the macOS command line tools one is fine; Homebrew Python is used when present).

## Install

1. Download `alfred-grep-files.alfredworkflow` from the [latest release](../../releases/latest).
2. Double-click it; Alfred opens the import sheet. Import.
3. Click **Configure Workflow…** on the workflow to set the keyword, search folder, exclusions, content search, limits, and editor command. The same window can be reopened from the workflow's context menu at any time.

The default search root is `~` (the home folder); set **Search folder** to whichever directory should be indexed.

## Use

| Key | Action |
| --- | --- |
| `f` (default) | Show the hint item (root, index size, exclusion source). |
| `f foo` | Fuzzy filename/folder matches, then content matches. |
| `⏎` | File: open with the default app. Folder: reveal in Finder. Content hit: open at the matched line. |
| `⌥ ⏎` | Reveal in Finder. |
| `⌘ ⏎` | Copy the absolute path; for content hits, `path:line`. |
| `Tab` | Alfred's native file actions + Quick Look. |

The keyword is a **Configuration Builder** field: change it under **Configure Workflow…** (default `f`). The Script Filter references it as `{var:grep_keyword}`.

Result rows lead with their type: **📁** folders, **📄** files, and **🔎** in-file content matches. The workflow ships its own `icon.png`, so Alfred shows the magnifier tile in the workflow list.

### Name matching

Names are ranked by fzf's path-aware fuzzy scorer (`--scheme=path`). fzf operators work in the name stage: `'exact`, `^prefix`, `suffix$`, `!negation`, and `one | other`. The list is capped at 20 name hits; folders are included and shown with a trailing `/`.

### Content matching

Content is a literal (fixed-string) grep. A multi-word query means *all words on the same line*, in any order. Query operators are stripped for the content stage. Results are capped at 10 items (`max_content_lines` per file). Huge files (> 5 MB) and binaries are skipped by ripgrep.

Content search is **deferred**: names paint immediately (~100 ms warm), then a background scan appends content hits through Alfred's `rerun` (typically within a second; hard scan budget 3 s). Results for an identical query are reused for 2 minutes.

### Exclusions

`excludes_mode` controls the source:

- `asimeow` (default) — read the [asimeow](https://github.com/mdnmdn/asimeow) config at `~/.config/asimeow/config.yaml` at invocation time and flatten `ignore[]` + every `rules[*].exclusions[]` into global directory names. If the config is absent or unreadable, the built-in list is used instead.
- `builtin` — use the built-in dependency-folder list.
- `none` — no exclusion beyond the workflow baseline.

Always excluded: `.git`, `.svn`, `.hg`, `.DS_Store`, and the workflow's own cache. Add anything else with `extra_excludes`. ripgrep's own `.gitignore` / `.ignore` / `.rgignore` handling still applies.

**Exclusion patterns.** `extra_excludes` accepts one entry per line (commas also work):

- **Names** — `build`, `node_modules`: skipped wherever a path component has that name.
- **Path patterns** — `Media/Audio Files`: matched at any depth, so `Project/Media/Audio Files` is skipped too. Leading `/` is ignored; entries are relative patterns, not absolute paths.
- **Extensions / suffixes** — `.logicproject` or `*.logicproject`: matches `Song.logicproject`, so Logic project packages are skipped.
- **Globs** — `Caches/**`, `*.tmp`: ripgrep `-g` syntax, matched at any depth when no `**/` is present.

All pruning happens in the ripgrep index and content scans. fzf's filter mode only fuzzy-matches the already-filtered path list; its `--walker-skip` can skip directory names during fzf's own tree walk, but that is not used here and cannot express path patterns like `Media/Audio Files`.

## Configuration (Configure Workflow…)

Open the workflow and click **Configure Workflow…**. Defaults live in `info.plist`; Alfred saves changed values to the workflow's `prefs.plist`.

| Field | Variable | Default | Meaning |
| --- | --- | --- | --- |
| Keyword | `grep_keyword` | `f` | Alfred trigger keyword. |
| Search folder | `search_root` | `~` | Root to index and search; `~` is the home folder. |
| Excluded folders | `excludes_mode` | `asimeow` | asimeow rules, built-in dev list, or no extra exclusions. |
| asimeow config path | `asimeow_config` | `~/.config/asimeow/config.yaml` | Exclusion source used in asimeow mode. |
| Extra excluded folders | `extra_excludes` | empty | Names, path patterns, or globs, one per line or comma-separated. |
| File contents | `content_enabled` | on | Turn off in-file search. |
| Maximum results | `max_items` | `30` | Total items emitted (name hits always above content). |
| Content hits per file | `max_content_lines` | `3` | Matching lines per file. |
| Folders | `include_dirs` | on | Emit folder results. |
| Editor command | `editor_command` | empty | Template for open-at-line, e.g. `code -g {path}:{line}:{col}`. Empty = auto-detect `code`, `cursor`, `zed`, `subl`, else `open`. |
| Index refresh interval | `index_ttl_seconds` | `600` | Rebuild the name index in the background after this age. |

Those field labels, help texts, and defaults are declared in `info.plist`, so Alfred renders the same documentation in **Configure Workflow…**; the workflow's embedded readme repeats the field list. For the long form — exclusion examples, editor templates, the plist schema, adding a field, precedence, and troubleshooting — see [CONFIGURATION.md](CONFIGURATION.md). It ships inside the `.alfredworkflow` archive next to this README.

## How it works

- `search.sh` resolves a `python3` for Alfred's minimal `PATH` and execs `search.py`.
- `search.py` maintains `paths.txt` (one root-relative path per line) in Alfred's workflow data folder; it is rebuilt with `rg --files --hidden` plus the exclusion globs, with stale-while-revalidate and an advisory lock. Directories are derived from the file list, so no second walk.
- Name search pipes the index through `fzf --filter … --scheme=path`.
- Content search runs in a detached background process (`search.py --content-scan <query-hash>`), writes a small JSON result keyed by root + query, and the Script Filter reads it on the next `rerun`.
- `action.sh` → `action.py` reads the item variables (`action`, `line`, `col`) and opens, reveals, or copies.

Only local tools are used. There are no daemons, launch agents, or network calls.

## Development

Run the Script Filter backend directly:

```bash
export alfred_workflow_data=/tmp/agf-data
export alfred_workflow_cache=/tmp/agf-cache
export search_root=/tmp/agf-test

./search.sh "alpha beta" | python3 -m json.tool
```

- `AGF_DEBUG=1` appends timings and scan errors to `search.py`'s stderr, which Alfred shows in the workflow debugger (Workflow → [🐞]).
- `search.py --rebuild` forces an index rebuild.
- The index and content caches live in `alfred_workflow_data` / `alfred_workflow_cache`, so deleting those folders is always safe.

To run from a checkout, symlink it into Alfred's workflows directory; Alfred executes the checked-out scripts directly. `./build.sh` produces the distributable archive locally. Release archives are built by CI from version tags.

`test.sh` runs a self-contained smoke test against a throwaway tree under `/tmp`:

```bash
./test.sh
```

## Known limitations

- Empty directories do not appear: only parents of indexed files are derived.
- asimeow exclusions are flattened globally, so any directory literally named `build`, `Library`, etc. is skipped even when a rule's marker file is absent.
- fzf is required; without it filename search cannot run (the workflow shows an install hint rather than half-working).
- No PDF/Office/archive text extraction, no regex mode, no content index — every content result is fresh from ripgrep.
- Content hits can duplicate a file already shown by name; the lines are still useful, so both are kept.
