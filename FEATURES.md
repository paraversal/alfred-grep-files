# alfred-grep-files — Feature brief (v0)

**One-liner:** type `f <query>` in Alfred and get fuzzy file/folder hits plus in-file grep hits from a chosen root (default `~`), with file-system results ranked above content, junk folders skipped via the asimeow config.

Status: implemented (v1). Everything below is the original spec; 💡 **Idea** callouts that remain unimplemented are still marked as ideas.

- **Platform:** macOS, Alfred 5 (`5.8.1` verified locally).
- **Matchers:** fzf `0.61.1` (`--filter --scheme=path`) for filenames; ripgrep `14.1.1` for content. Both already installed via Homebrew (`/opt/homebrew/bin`).
- **No external services, no indexing of file contents** (content is grepped live per query).

## 1. Trigger & scope

| Trigger | Behaviour |
| --- | --- |
| `f ` (empty query) | Short hint item: what to type, current root, indexed file count, config path. |
| `f <query>` | Unified search: file + folder matches, then content matches (see §2, §3). |

- **Keyword:** `f` by default and user-changeable in **Configure Workflow…** (a Configuration Builder field named `grep_keyword`; the Script Filter references it as `{var:grep_keyword}`). Help copy avoids repeating the literal keyword.
- **Search root:** `~` by default. A Configuration Builder field named `search_root`; `~` expanded; resolved with `realpath`; must be a directory (else an error item shows the offending path).
- **Recursive, all depths.** Symlinks are not followed (loop safety). Hidden files are included in the filename index, so `.zshrc`, `.config/…` are findable.
- **Case:** smart case — an all-lowercase query is case-insensitive; any uppercase character makes the query case-sensitive (fzf behaviour, matches §2).
- **Unicode:** paths and queries are normalized to NFC before matching, so accented macOS (NFD) filenames match typed NFC text.
- Root is re-read from config on every invocation; nothing is hardcoded to this Mac.

💡 **Idea — scoped queries:** support typed prefixes that don't collide with fzf's own query syntax: `p:foo` = path hits only (files + folders, no content), `c:foo` = content only, `dir:…` to temporarily root the search elsewhere (e.g. `dir:~/Downloads invoice`), and `@` to use the front Finder window's folder as root. Cheap to add, removes the need for extra keywords.

## 2. Name matching — files and folders (fzf, "very fuzzy")

Name matching is **delegated to fzf** in filter mode instead of a hand-rolled scorer: fzf already ships path-aware fuzzy scoring, multi-term AND, smart case, and its exact/prefix/negation operators — and it is fast enough to score the whole tree per keystroke (measured ~25–30 ms over 200k paths, warm). This is the answer to the "very fuzzy" requirement: no substring-only matching, no manual scoring pass to tune.

- **Pipeline:** cached path list (one path per line, relative to the root) → `fzf --filter "$query" --scheme=path | head -n 20`.
- **Semantics:** ordered subsequence matching (`agf` → `Coding/git/alfred-grep-files/README.md`, `dbconf` → `db-config.toml`, `dot cfg` → `dotfiles/asimeow/.config`); whitespace separates terms that must all match, order-independent; `--scheme=path` adds bonuses at path-component boundaries and match positions, so the basename outweighs directory text; smart case (all-lowercase query is case-insensitive).
- **fzf operators come for free:** `'exact`, `^prefix`, `suffix$`, `!negation`, `a | b` OR terms. They apply to the filename stage only; the content stage strips them (§3).
- **Ordering:** fzf score order. Tiebreak stays fzf's default (`length`), so equal-score hits prefer shorter paths; with an unchanged index the order is stable across keystrokes.
- **Cap:** top 20 name hits (files + folders), cap enforced with `head` (fzf stops early on SIGPIPE, so huge match sets are cheap). The remaining item budget goes to content hits (§3), so a content query is never squeezed out by weak name matches.
- **Directories are results too:** the index also contains directory paths, scored in the same fzf pass. They render with a trailing `/` and ⏎ reveals them in Finder. Toggle: `include_dirs` (default `true`).
- Directory coverage comes from deriving every parent of each indexed file, so non-empty directories are complete and need no second scan; a directory whose files are all excluded (e.g. a project's `node_modules`) correctly disappears. Empty directories are the only gap (💡 idea below).

💡 **Idea — empty directories:** if empty-folder results matter, add a `find <root> -type d` pass to the index build (or `fd --type d`, already installed) — cheap, but it adds a second walk over the tree.
💡 **Idea — fallback/bundling:** fzf is a dependency; if it is missing, show a fix-it item rather than silently degrading. A later distributable build can bundle the MIT-licensed fzf universal binary inside the workflow, keeping the machine dependency-free (§10).
💡 **Idea — typo tolerance:** fzf needs characters in order, so transpositions/typos ("recieve") still fail. If a query yields < 3 hits, retry once with a bounded edit-distance pass over basenames ("did you mean…").
💡 **Idea — recency boost:** fzf's last tiebreak is input order. Sort the index by mtime descending (or add a small recently-modified set) so equally-scored hits prefer fresh files, without ever crossing the file/content tier (§4).

## 3. Content search

Run `rg` per query over the root, always limited:

```
rg --no-messages --no-heading --line-number --column --color never \
   --smart-case --fixed-strings --max-count 3 --max-filesize 5M \
   -e <token1> [--and -e <token2> …] \
   -g '!**/<excluded>' … <root>
```

- **Literal, not regex:** the query is fixed-string; typing `foo(` must not error. Multi-token query means all tokens on the *same line* (`--and`), order-independent.
- **Operators:** fzf syntax is filename-only. For content, `'`, `^`, `$` and `|` are stripped and only positive terms are grepped; `!term` is ignored rather than misread as a literal (💡 idea: honor it as an rg exclusion).
- **Smart-case:** lowercase query = insensitive; any uppercase = sensitive.
- **Per file:** at most 3 matching lines (`max_content_lines`), files > 5 MB skipped, binaries skipped by rg, excludes applied (see §5).
- **Output:** one result item per line — up to 10 content items per query in v0, appended after the max 20 name hits (30 items total).
- **Snippet:** the line trimmed, tabs collapsed, trimmed around the first match to ~100 chars with `…` where cut.
- **Suppression:** 💡 idea — hide content hits from files already shown among the filename hits when the list is dense; today both can appear (file item + line items), which is predictable but slightly redundant.

## 4. Ranking & merging

- **Hard tier rule:** every file/folder hit outranks every content hit, as requested. The final list is `[file and folder hits…] + [content hits…]`, both internally ranked.
- Content hits are ranked by: line quality (whole-word > word-start > substring), then file recency, then shorter path. fzf scores are not exposed in filter mode, so the content tier uses its own light heuristic rather than reusing fzf's ranking.
- If the item cap is hit, the last item is a non-actionable note: `showing top 30 — type more to narrow`. (No exact count: fzf does not expose total matches cheaply in filter mode.)
- Alfred's knowledge-based reordering is disabled (`skipknowledge: true`), so the ranking above is exactly what the user sees; learned boosting, if added, happens in our own ordering layer (see the recency idea).

💡 **Idea — soft interleave:** if filename hits are plentiful but weak (short, scattered fzf matches) while content hits are strong, mix 2–3 content hits in after the first 6 filename hits. Keep it opt-in via `rank_mode: tiered|mixed`; tiered stays the default.

## 5. Exclusions (asimeow)

Source of truth: `~/.config/asimeow/config.yaml` (also the file the user already maintains for Time Machine/restic marks). Missing file → built-in fallback list; unreadable/invalid → fallback + a one-line note in Alfred's debug log, search still works.

- Read at invocation time, cached by mtime/age; the file is tiny.
- Parse only the schema we need, tolerating unknown keys (no YAML dependency): `ignore[]` (e.g. `.git`, `Library`) and every `rules[*].exclusions[]` (e.g. `node_modules`, `target`, `.venv`, `build`, `_build`, `Pods`).
- Flatten to a set of **directory names**, then pass one `-g '!**/<name>'` negation per name to rg, and apply the same set when building the file index.
- **Workflow baseline, independent of asimeow:** `.git`, `.svn`, `.hg`, `.DS_Store`, and the workflow's own cache directory. Do not rely on asimeow for this — its `ignore` entries are evaluated relative to its `roots`, so `.git` directories nested under `~/...` are not guaranteed to be marked (the user's read of the config is the same: `.git` is not excluded asimeow-side).
- **Extra excludes, always available:** `extra_excludes` Configuration Builder field — comma/newline-separated names, path patterns, or rg globs applied on top of everything (e.g. `Library/Application Support`, `.logicproject`, `*.nosync`, a private `archive` folder). A bare name matches any path component; a path pattern is matched at any depth (internally prefixed with `**/`, so it is not relative to the index root or process cwd); a dot-suffix like `.logicproject` matches `Song.logicproject`. Leading `/` is ignored; absolute paths are not supported. Empty by default; meant for the folders asimeow will never know about.
- **fzf's filter mode has no directory exclusion:** it only fuzzy-scores lines from the index; `--walker-skip` skips directory *names* only during fzf's own walk, which this workflow does not use. All pruning happens in rg's `-g` globs during index and content scans, so exclusions stay O(walk) and never leak into fzf's scoring.
- **Documented tradeoff:** asimeow's rule semantics are per-project (a name is excluded only where its marker file exists); the search applies the flattened names *globally*. That prunes the massive folders, at the cost of also hiding e.g. any unrelated directory literally named `build` or `Library`.
- **Do not fight rg's own ignore handling:** `.gitignore`, `.ignore`, `.rgignore` are still respected for content search and index building, so a repo's own ignore rules also apply — one less thing to configure.
- **Decided (2026-11-10):** flattened names are accepted as-is. The per-project variant (exclude a name only when the matching rule's marker file sits next to it) is rejected for v0 — it costs a walk per candidate for a marginal win. Escape hatch if a false exclusion ever bites: an `include_overrides` pattern, not a semantic change.

## 6. Result items

**File item**

| Field | Value |
| --- | --- |
| title | `📄` + basename (`📄 README.md`) |
| subtitle | parent path relative to root (`Coding/git/alfred-grep-files`) |
| icon | none set — the title emoji is the row marker; `"type":"file"` keeps native actions |
| quicklookurl | absolute path (Tab → Quick Look works natively) |
| text.copy | absolute path |
| arg | absolute path |

**Directory item**

| Field | Value |
| --- | --- |
| title | `📁` + basename with trailing slash (`📁 alfred-grep-files/`) |
| subtitle | parent path relative to root |
| icon | none set — the title emoji is the row marker |
| quicklookurl | absolute path |
| text.copy | absolute path |
| arg | absolute path |
| ⏎ | reveal in Finder |

**Content item**

| Field | Value |
| --- | --- |
| title | `🔎` + line snippet (`🔎 func main() {`) |
| subtitle | `relative/path.md:42` |
| icon | none set — the title emoji marks an in-file match |
| quicklookurl | the file (Quick Look shows the file, not the line — see idea) |
| text.copy | `relative/path.md:42` |
| arg / variables | absolute path in `arg`, `{"line": "42", "col": "8"}` in item `variables` |

Row type is carried by a title emoji (📁 folder, 📄 file, 🔎 content); the JSON still uses `"type":"file"` so Tab exposes Alfred's native file actions and Quick Look.

The workflow ships a 512×512 `icon.png` (warm paper tile, dark magnifier, amber lens). Alfred uses it as the workflow icon automatically; `build.sh` includes it when present.

- Tab additionally opens Alfred's native action panel for the file or folder (Open With, Reveal in Finder, Move to…), which is the main reason `arg` is a real path rather than a display string.
- 💡 **Idea — snippet preview:** for content hits, write a small cached `.txt` snippet (the line ± 3 lines, with the line number) into the workflow data dir and point `quicklookurl` at that, so Tab previews the actual context. Cleanup on age.

## 7. Actions

| Key | Action |
| --- | --- |
| ⏎ | File: open with default app. Folder: reveal in Finder. Content: open at the matched line. |
| ⌥ ⏎ | Reveal in Finder (`open -R`). |
| ⌘ ⏎ | Copy absolute path (`path` for files/folders, `path:line` for content hits). |
| Tab | Alfred's native file actions + Quick Look. |

- **Open at line (core):** content items carry `line`/`col`; ⏎ substitutes `{path}`/`{line}` into `editor_command`. With no template set, the workflow picks the first editor found on disk: `code -g`, `cursor -g`, `zed`, `subl`. `code` is installed here, so "VS Code, go to line" works out of the box. If no editor is found, fall back to `open` on the file (the subtitle still shows `:line`). A configured editor that disappears falls back the same way and logs once in Alfred's debug log.
- 💡 **Idea — copy variants:** ⇧⌘ copy the *relative* path; ⌥⌘ copy only the filename; on content hits ⌃⏎ copy the matching line text. All cost nothing to implement once mods exist.
- 💡 **Idea — multi-open:** a trailing “Open all N results” item (valid only when N ≤ 5) that hands all paths to the configured editor. Alfred script filters can't multi-select natively, so this is the closest thing.

## 8. Performance envelope

Targets on this machine for a ~50–200k file tree:

| Stage | Budget |
| --- | --- |
| First paint (name results) | ≤ 80 ms warm; ~0.6 s cold (one-time index build) |
| Content results appended | ≤ 400 ms typical; background scan hard cap 3 s |
| Absolute worst case | 2 s, then degrade to name-only results |

- **File index:** a flat newline file (`paths.txt`: one root-relative path per line) in Alfred's workflow data dir, rebuilt with `rg --files --hidden` plus the exclusion globs; directory paths are derived from that list (parents of every file), so indexing folders adds no extra walk. Measured cost on this machine: 122,517 files under a large subtree in ~0.55 s. Stale-while-revalidate: serve the old index immediately, refresh in the background when older than `index_ttl_seconds` (600 s default). An advisory lock prevents duplicate rebuilds. This is the only expensive step; fzf scoring is ~25–30 ms (§2).
- **Cold start:** no index yet → show an `Indexing <root>…` item (non-actionable) and build once; subsequent keystrokes are warm.
- **Cancellation:** Alfred kills the previous script-filter process group on each keystroke. `search.sh` `exec`s Python, so foreground `fzf` and `rg` inherit that group. The content-scan supervisor is detached, but its `rg` inherits the supervisor's group and the next query kills that whole group; a dead `scan.pid` is also cleaned up on sight, so no orphaned scans survive.
- **Bounded work:** every cap is enforced in the pipeline (`head` on fzf output, `--max-count`/`--max-filesize` on rg, a ~4 MiB stdout capture and 10k parsed-hit cap for content scans, item cap before JSON), not by hoping either tool is fast.
- Everything runs locally; no daemons, no launch agents.

✅ **Implemented — deferred content:** name results paint immediately, then a detached `search.py --content-scan <query-hash>` process writes a query-keyed JSON cache and the Script Filter appends those hits on an Alfred `rerun` (0.35 s). Results are reused for 2 minutes; the scan has a 3 s budget. Noticeably snappier on huge trees, at the cost of a second fetch.
💡 **Idea — index freshness:** an optional launchd/FSEvents-triggered refresh replaces the TTL heuristic. TTL is fine for v0; this only matters if the index feels stale.

## 9. Configuration

All via Alfred's **Configuration Builder** (open the workflow → **Configure Workflow…**), no config file of our own. Defaults live in `info.plist`; Alfred saves changed values to `prefs.plist` (gitignored), and exports each field to scripts as an environment variable.

The in-app help is part of the shipped plist, not separate prose: each field's label and description in `userconfigurationconfig` is rendered by Alfred in **Configure Workflow…**, and the workflow's embedded `readme` repeats the field list. [`CONFIGURATION.md`](CONFIGURATION.md) is the long-form reference — configuration layers, exclusion and editor examples, the plist schema for adding fields, precedence/clamping, non-configurable limits, and troubleshooting — and is included in the `.alfredworkflow` archive.

| Field | Variable | Default | Meaning |
| --- | --- | --- | --- |
| Keyword | `grep_keyword` | `f` | Alfred trigger keyword. |
| Search folder | `search_root` | `~` | Root to index/grep. |
| Excluded folders | `excludes_mode` | `asimeow` | `asimeow` \| `builtin` \| `none`. |
| asimeow config path | `asimeow_config` | `~/.config/asimeow/config.yaml` | Exclusion source. |
| Extra excluded folders | `extra_excludes` | (empty) | Additional names, path patterns, or globs on top of asimeow. |
| File contents | `content_enabled` | `true` | Kill switch for in-file grep. |
| Maximum results | `max_items` | `30` | Items emitted (≤ 20 name + ≤ 10 content; Alfred shows 9 at a time). |
| Content hits per file | `max_content_lines` | `3` | Matching lines per file. |
| Folders | `include_dirs` | `true` | Emit directory results alongside files. |
| Editor command | `editor_command` | (empty) | Template for open-at-line, e.g. `code -g {path}:{line}`; empty = auto-detect `code`/`cursor`/`zed`/`subl`. |
| Index refresh interval | `index_ttl_seconds` | `600` | Index staleness threshold. |

Everything above is editable in the same Configuration Builder window, so `Workflow → Configure Workflow…` is the only configuration surface users need.

## 10. Failure behaviour (never a blank Alfred panel)

- `rg` missing → item: “ripgrep not found — brew install ripgrep”, non-actionable.
- `fzf` missing → item: “fzf not found — brew install fzf”, name search is the primary path and must not half-work (content search is optional and disabled meanwhile).
- Binary lookup: Alfred runs script filters with a minimal `PATH`, so resolve `rg`/`fzf` from `PATH` plus `/opt/homebrew/bin` and `/usr/local/bin`; never assume the user's shell startup files.
- `search_root` missing/not a dir → item showing the resolved path and a hint.
- asimeow config missing/broken → builtin excludes, search proceeds.
- Index rebuild fails (permissions, I/O) → fall back to a live `rg --files` scan for this query, with the error only in the debug log.
- Any unexpected exception → one error item with the message; exit code 0 with valid JSON always (Alfred swallows non-JSON silently).

## 11. Explicit non-goals for v0

- No editing/replacing/naming; this is a finder.
- No bespoke fuzzy scorer: fzf is the ranking authority for names (idea above covers bundling it for portability).
- No content indexing (mdfind/Spotlight, SQLite FTS) — grep only, so results are always fresh.
- No PDF/Office/archive text extraction (💡 idea: `textutil`/`pdftotext` fallback for known types, only if it proves useful).
- No regex content mode, no boolean NOT/OR operators (💡 idea: `re:` prefix for regex, `-term` to negate).
- No sync/multi-machine settings; single Mac, single root.

## Settled questions (2026-11-10)

1. **Keyword:** `f` by default; users can change it in Alfred's workflow editor. Nothing downstream depends on the literal.
2. **⏎ on a content hit:** open the file at the matched line (auto-detected editor, `editor_command` override).
3. **Folders:** first-class results, `include_dirs: true`.
4. **asimeow exclusions:** flattened globally is accepted; per-project semantics are explicitly out of scope for v0.
