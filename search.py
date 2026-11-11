#!/usr/bin/env python3
"""Grep Files: Alfred script-filter backend.

Searches a directory by path name (fzf) and by file content (ripgrep).
Designed for Alfred's Script Filter with "input as argv" (query in argv[1])
and a minimal environment. Must always print one JSON object and exit 0.
"""

import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import traceback
import unicodedata
from pathlib import Path

HOME = os.path.expanduser("~")
HERE = Path(__file__).resolve().parent
BUNDLE_ID = "dev.paraversal.alfred-grep-files"

NAME_LIMIT = 20
CONTENT_LIMIT = 10
INDEX_VERSION = 2
CONTENT_SCAN_TIMEOUT = 3.0  # background content scan budget
CONTENT_CAPTURE_BYTES = 4 * 1024 * 1024  # cap one scan's rg output
CONTENT_HIT_TEXT = 4096  # chars of a line kept in cache/UI
CONTENT_PARSE_HITS = 10000  # stop parsing once this many matches are seen
CONTENT_SCAN_STALE = 8.0  # replace a same-query scan older than this
CONTENT_CACHE_TTL = 120  # reuse identical-query content results
FZF_TIMEOUT = 1.5
BUILD_TIMEOUT = 60

DATA = Path(
    os.environ.get("alfred_workflow_data")
    or os.path.join(HOME, "Library/Application Support/Alfred/Workflow Data", BUNDLE_ID)
)
CACHE = Path(
    os.environ.get("alfred_workflow_cache")
    or os.path.join(HOME, "Library/Caches/com.runningwithcrayons.Alfred/Workflow Data", BUNDLE_ID)
)

BASELINE_EXCLUDES = [".git", ".svn", ".hg", ".DS_Store"]
# Used when excludes_mode=builtin, or when the asimeow config is unreadable.
BUILTIN_EXCLUDES = [
    ".git", ".svn", ".hg", ".DS_Store",
    "node_modules", "dist", ".svelte-kit",
    "target", "vendor", "obj", "bin", "packages",
    "__pycache__", "__pypackages__", "build", ".venv", "venv",
    ".godot", ".vagrant", "bower_components", ".stack-work",
    "Carthage", "Pods", "_build", "debug", "Library",
]

TOOL_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin", "/bin"]


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #

def _env(name, default=""):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _bool_env(name, default):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name, default, minimum, maximum):
    try:
        value = int(_env(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _debug(message):
    if not _bool_env("AGF_DEBUG", False):
        return
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        with open(str(CACHE / "search.log"), "a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except Exception:
        pass


_TOOLS = {}


def find_tool(name):
    """Find an executable, since Alfred's script environment has a tiny PATH."""
    if name in _TOOLS:
        return _TOOLS[name]
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    dirs.extend(TOOL_DIRS)
    seen = set()
    for directory in dirs:
        try:
            directory = os.path.realpath(directory)
        except Exception:
            continue
        if directory in seen:
            continue
        seen.add(directory)
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            _TOOLS[name] = candidate
            return candidate
    _TOOLS[name] = None
    return None


def display_path(path):
    path = os.path.normpath(path)
    if path == HOME:
        return "~"
    if path.startswith(HOME + os.sep):
        return "~" + path[len(HOME):]
    return path


def real_cache_dir():
    try:
        return os.path.realpath(str(CACHE))
    except Exception:
        return None


def under(path, parent):
    if not parent:
        return False
    try:
        return os.path.commonpath([os.path.abspath(path), parent]) == parent
    except ValueError:
        return False


def nfc(text):
    return unicodedata.normalize("NFC", text)


def actual_path(root, rel):
    """Map an NFC index path back to the on-disk spelling.

    macOS stores accented names as NFD in most APIs; APFS resolves either form,
    but external volumes may not, so try both before giving up.
    """
    for candidate in (rel, nfc(rel), unicodedata.normalize("NFD", rel)):
        absolute = os.path.join(root, candidate)
        if os.path.exists(absolute):
            return absolute
    return os.path.join(root, rel)


def emit(payload):
    sys.stdout.buffer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


def item(title, subtitle="", valid=False, **extra):
    result = {"title": title, "subtitle": subtitle, "valid": valid}
    result.update(extra)
    return result


def error_emit(title, subtitle):
    emit({"items": [item(title, subtitle, valid=False)]})


def run_capture(argv, timeout, cwd=None):
    """Run a command, returning (stdout_bytes, stderr_bytes, returncode).

    The child stays in our process group, so Alfred's process-group kill
    reaches it too. On timeout only the command itself is killed (it has no
    children); partial output is returned.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        out, err = proc.communicate()
        return out or b"", err or b"", None
    return out or b"", err or b"", proc.returncode


def run_scan_capture(argv, timeout, byte_limit):
    """Run a content search with a wall-clock and output budget.

    Unlike run_capture this streams stdout and stops at `byte_limit`. A broad
    query can make rg produce tens of megabytes that would otherwise be read
    in full and parsed only to discard all but ten hits. The child also stays
    in our process group: when the next query kills the scan supervisor, the
    pgid kill reaches rg instead of orphaning it.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    chunks = []
    total = 0
    deadline = time.monotonic() + timeout
    fd = proc.stdout.fileno()
    try:
        while total < byte_limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except OSError:
                break
            if not ready:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
    data = b"".join(chunks)
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        data = data[: cut + 1] if cut >= 0 else b""
    return data


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def search_root():
    raw = _env("search_root", "~")
    expanded = os.path.expanduser(os.path.expandvars(raw.strip() or "~"))
    return os.path.realpath(expanded)


def excludes_mode():
    mode = _env("excludes_mode", "asimeow").strip().lower()
    return mode if mode in ("asimeow", "builtin", "none") else "asimeow"


def asimeow_config_path():
    raw = _env("asimeow_config", "~/.config/asimeow/config.yaml")
    return Path(os.path.expanduser(os.path.expandvars(raw.strip())))


def _yaml_scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]
    else:
        # Drop an unquoted trailing comment.
        text = re.split(r"\s+#", text, maxsplit=1)[0].strip()
    return text.strip()


def parse_asimeow_config(path):
    """Extract ignore[] and rules[*].exclusions[] from asimeow's small YAML.

    asimeow writes an indentless list of rules:

        ignore:
        - .git
        rules:
        - name: node
          file_match: package.json
          exclusions:
          - node_modules

    This is a lenient subset parser; unparseable lines are skipped.
    """
    names = []
    section = None
    in_exclusions = False
    try:
        with open(str(path), "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return None

    top_key_re = re.compile(r"^([A-Za-z_][\w-]*):\s*(?:#.*)?$")
    rule_start_re = re.compile(r"^\s*-\s+[A-Za-z_][\w-]*\s*:")
    map_key_re = re.compile(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*)$")
    list_item_re = re.compile(r"^\s*-\s+(.+?)\s*$")

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        match = top_key_re.match(line)
        if match:
            section = match.group(1)
            in_exclusions = False
            continue

        if section == "ignore":
            match = list_item_re.match(line)
            if match:
                value = _yaml_scalar(match.group(1))
                if value and value not in ("~", "./"):
                    names.append(value)
            continue

        if section == "rules":
            if rule_start_re.match(line):
                in_exclusions = False
                continue
            match = map_key_re.match(line)
            if match:
                in_exclusions = match.group(1) == "exclusions" and not match.group(2).strip()
                continue
            if in_exclusions:
                match = list_item_re.match(line)
                if match:
                    value = _yaml_scalar(match.group(1))
                    if value and value not in ("~", "./"):
                        names.append(value)

    return names or []


def exclude_config():
    """Return (directory names, extra glob patterns)."""
    names = list(BASELINE_EXCLUDES)
    mode = excludes_mode()

    if mode == "asimeow":
        parsed = parse_asimeow_config(asimeow_config_path())
        names.extend(parsed if parsed else BUILTIN_EXCLUDES)
    elif mode == "builtin":
        names.extend(BUILTIN_EXCLUDES)

    extras = []
    raw = os.environ.get("extra_excludes", "") or ""
    for piece in re.split(r"[,\n]", raw):
        piece = piece.strip()
        if piece:
            extras.append(piece)

    # Stable de-dupe.
    seen = set()
    unique_names = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique_names.append(name)
    return unique_names, extras


_GLOB_SPECIAL = set("*?[]{}")


def glob_escape(name):
    return "".join("\\" + ch if ch in _GLOB_SPECIAL else ch for ch in name)


def exclude_globs(names, extras):
    """Translate ignore names/globs into ripgrep -g negation patterns."""
    globs = []
    seen = set()
    for name in names:
        pattern = "!**/" + glob_escape(name)
        if pattern not in seen:
            seen.add(pattern)
            globs.append(pattern)
    for extra in extras:
        extra = extra.strip()
        if extra.startswith("!"):
            extra = extra[1:].strip()
        if not extra:
            continue
        # A bare name is matched at any depth by basename. Anything that
        # looks like a path, glob, or dot-suffix is matched as a path suffix,
        # also at any depth; ripgrep treats slash patterns as cwd-relative
        # otherwise, which would only work at the top of the index root.
        if "/" not in extra and not any(ch in extra for ch in _GLOB_SPECIAL) and not extra.startswith("."):
            pattern = "!**/" + glob_escape(extra)
        else:
            path = extra.lstrip("/").rstrip("/")
            if not path:
                continue
            if extra.startswith(".") and "/" not in extra and not any(ch in extra for ch in _GLOB_SPECIAL):
                # ".logicproject" is the natural way to ask for an extension;
                # make it match Song.logicproject as well as the exact name.
                pattern = "!**/*" + extra
            elif path.startswith("**/"):
                pattern = "!" + path
            else:
                pattern = "!**/" + path
        if pattern not in seen:
            seen.add(pattern)
            globs.append(pattern)
    return globs


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #

def paths_file():
    return DATA / "paths.txt"


def meta_file():
    return DATA / "index.json"


def error_file():
    return DATA / "index.error"


def lock_file():
    return DATA / "index.lock"


def guard_file():
    return DATA / "spawn.guard"


def read_meta():
    try:
        return json.loads(meta_file().read_text(encoding="utf-8"))
    except Exception:
        return None


def read_error():
    try:
        text = error_file().read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    return text.splitlines()[0][:160] if text else ""


def write_json_atomic(path, payload):
    tmp = path.with_name(path.name + ".tmp.%d" % os.getpid())
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def lock_is_held():
    """True when a rebuild process appears to be alive."""
    lock = lock_file()
    try:
        raw = lock.read_text(encoding="utf-8").strip()
    except Exception:
        return False
    if not raw:
        # A lock without a pid is treated as live for a short grace period.
        try:
            return time.time() - lock.stat().st_mtime < 30
        except Exception:
            return False
    try:
        pid = int(raw.split()[0])
    except ValueError:
        return False
    if _pid_alive(pid):
        return True
    try:
        lock.unlink()
    except OSError:
        pass
    return False


def acquire_lock():
    try:
        DATA.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        fd = os.open(str(lock_file()), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except OSError:
        if lock_is_held():
            return False
        try:
            os.unlink(str(lock_file()))
        except OSError:
            pass
        try:
            fd = os.open(str(lock_file()), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except OSError:
            return False
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def release_lock():
    try:
        os.unlink(str(lock_file()))
    except OSError:
        pass


def rg_files(rg, root, globs):
    argv = [rg, "--files", "--hidden"]
    for pattern in globs:
        argv.extend(["-g", pattern])
    argv.append(root)
    out, err, code = run_capture(argv, BUILD_TIMEOUT)
    if code not in (0, 1):
        message = err.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(message[-1] if message else "ripgrep failed (exit %s)" % code)
    return out.decode("utf-8", "replace").splitlines()


def build_index(root, names, extras):
    """Build paths.txt (files then directories) and index.json."""
    rg = find_tool("rg")
    if not rg:
        raise RuntimeError("ripgrep is not installed")

    globs = exclude_globs(names, extras)
    cache_real = real_cache_dir()
    files = []
    seen = set()
    for raw in rg_files(rg, root, globs):
        raw = raw.strip()
        if not raw:
            continue
        path = raw if os.path.isabs(raw) else os.path.join(root, raw)
        if under(path, cache_real):
            continue
        rel = os.path.relpath(path, root)
        if rel == "." or rel.startswith(".." + os.sep) or rel == "..":
            continue
        rel = nfc(rel.replace(os.sep, "/"))
        if rel not in seen:
            seen.add(rel)
            files.append(rel)

    directories = set()
    for rel in files:
        parent = os.path.dirname(rel)
        while parent:
            if parent in directories:
                break
            directories.add(parent)
            parent = os.path.dirname(parent)

    DATA.mkdir(parents=True, exist_ok=True)
    tmp_paths = paths_file().with_name("paths.txt.tmp.%d" % os.getpid())
    with open(str(tmp_paths), "w", encoding="utf-8") as handle:
        for rel in files:
            handle.write(rel + "\n")
        for rel in sorted(directories):
            handle.write(rel + "\n")
    os.replace(str(tmp_paths), str(paths_file()))

    write_json_atomic(
        meta_file(),
        {
            "version": INDEX_VERSION,
            "root": root,
            "excludes": sorted(names) + ["extra:" + extra for extra in extras],
            "files": len(files),
            "dirs": len(directories),
            "built": time.time(),
        },
    )
    return len(files), len(directories)


def rebuild_main():
    if not acquire_lock():
        return 0
    try:
        try:
            os.unlink(str(error_file()))
        except OSError:
            pass
        root = search_root()
        names, extras = exclude_config()
        files, dirs = build_index(root, names, extras)
        _debug("index built: %d files, %d dirs" % (files, dirs))
    except Exception as exc:
        _debug("index build failed: %s\n%s" % (exc, traceback.format_exc()))
        try:
            DATA.mkdir(parents=True, exist_ok=True)
            error_file().write_text(str(exc), encoding="utf-8")
        except Exception:
            pass
    finally:
        release_lock()
    return 0


def spawn_rebuild():
    """Start a detached rebuild, at most once every few seconds."""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    guard = guard_file()
    try:
        if guard.exists() and time.time() - guard.stat().st_mtime < 5:
            return False
    except OSError:
        pass
    try:
        guard.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return False
    try:
        log = open(str(DATA / "rebuild.log"), "ab")
    except OSError:
        return False
    try:
        subprocess.Popen(
            [sys.executable, str(HERE / "search.py"), "--rebuild"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=os.environ.copy(),
        )
    except Exception:
        _debug("could not spawn rebuild: %s" % traceback.format_exc())
        return False
    finally:
        log.close()
    return True


def index_state(root, names, extras):
    paths = paths_file()
    meta = read_meta()
    if not paths.is_file() or not meta:
        return "missing"
    if meta.get("version") != INDEX_VERSION:
        return "missing"
    if meta.get("root") != root:
        return "missing"
    signature = sorted(names) + ["extra:" + extra for extra in extras]
    if meta.get("excludes") != signature:
        return "stale"
    ttl = _int_env("index_ttl_seconds", 600, 0, 86400)
    try:
        age = time.time() - paths.stat().st_mtime
    except OSError:
        return "missing"
    return "stale" if age > ttl else "fresh"


def live_listing(root, names, extras):
    """Build an in-memory listing when the index is unavailable."""
    rg = find_tool("rg")
    if not rg:
        return None, "ripgrep is not installed"
    try:
        raw_paths = rg_files(rg, root, exclude_globs(names, extras))
    except Exception as exc:
        return None, str(exc)
    cache_real = real_cache_dir()
    files = []
    seen = set()
    for raw in raw_paths:
        raw = raw.strip()
        if not raw:
            continue
        path = raw if os.path.isabs(raw) else os.path.join(root, raw)
        if under(path, cache_real):
            continue
        rel = nfc(os.path.relpath(path, root).replace(os.sep, "/"))
        if rel == "." or rel == ".." or rel.startswith("../"):
            continue
        if rel not in seen:
            seen.add(rel)
            files.append(rel)
    directories = set()
    for rel in files:
        parent = os.path.dirname(rel)
        while parent:
            if parent in directories:
                break
            directories.add(parent)
            parent = os.path.dirname(parent)
    return files + sorted(directories), None


# --------------------------------------------------------------------------- #
# Query parsing
# --------------------------------------------------------------------------- #

def content_tokens(query):
    tokens = []
    for raw in query.split():
        if raw in ("|", "||"):
            continue
        token = raw
        if token.startswith("!"):
            continue
        token = token.lstrip("'^")
        token = token.rstrip("$")
        if token:
            tokens.append(nfc(token))
    return tokens


def smart_case(tokens):
    """True when matching should be case-sensitive (any uppercase)."""
    for token in tokens:
        if any(ch.isupper() for ch in token):
            return True
    return False


def content_args(rg, root, tokens, globs, max_lines, fixed):
    argv = [
        rg,
        "--no-messages",
        "--no-heading",
        "--line-number",
        "--column",
        "--color", "never",
        "--smart-case",
        "--max-count", str(max_lines),
        "--max-filesize", "5M",
        "--field-match-separator", "\x1f",
        "--hidden",
    ]
    if not fixed:
        argv.append("--pcre2")
    for pattern in globs:
        argv.extend(["-g", pattern])
    for token in tokens:
        argv.extend(["-e", token])
    argv.append("--")
    argv.append(root)
    return argv


# --------------------------------------------------------------------------- #
# fzf
# --------------------------------------------------------------------------- #

def fzf_results(fzf, query, source, limit):
    """Return up to `limit` matches from fzf's filter mode.

    `source` is a binary file object or bytes. FZF_DEFAULT_OPTS is cleared so
    personal fzf defaults can't change the output format.
    """
    query = nfc(query)
    env = os.environ.copy()
    env.pop("FZF_DEFAULT_OPTS", None)
    env["FZF_DEFAULT_OPTS_FILE"] = ""
    proc = subprocess.Popen(
        [fzf, "--filter=" + query, "--scheme=path"],
        stdin=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    results = []
    pending = b""
    deadline = time.monotonic() + FZF_TIMEOUT
    fd = proc.stdout.fileno()
    try:
        while len(results) < limit:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except OSError:
                break
            if not ready:
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending and len(results) < limit:
                line, pending = pending.split(b"\n", 1)
                value = line.decode("utf-8", "replace").rstrip("\r")
                if value:
                    results.append(value)
        if pending and len(results) < limit:
            value = pending.decode("utf-8", "replace").rstrip("\r")
            if value:
                results.append(value)
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
    return results


# --------------------------------------------------------------------------- #
# Items
# --------------------------------------------------------------------------- #

def base_variables(line, col, action="open"):
    return {"action": action, "line": str(line), "col": str(col)}


def with_mods(variables):
    mods = {}
    for key, label, action in (
        ("alt", "Reveal in Finder", "reveal"),
        ("cmd", "Copy path", "copy"),
    ):
        mod_vars = dict(variables)
        mod_vars["action"] = action
        mods[key] = {"valid": True, "subtitle": label, "variables": mod_vars}
    return mods


def path_item(rel, absolute, is_dir, root):
    name = nfc(os.path.basename(absolute.rstrip(os.sep))) or display_path(absolute)
    if is_dir:
        name = "📁 " + name + "/"
    else:
        name = "📄 " + name
    parent = os.path.dirname(rel)
    subtitle = display_path(os.path.join(root, parent)) if parent else display_path(root)
    line = 0
    variables = base_variables(line, 0)
    result = {
        "type": "file",
        "title": name,
        "subtitle": subtitle,
        "arg": absolute,
        "quicklookurl": absolute,
        "text": {"copy": absolute, "largetype": absolute},
        "variables": variables,
        "mods": with_mods(variables),
    }
    return result


def snippet(text, tokens, case_sensitive, width=110):
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= width:
        return text
    haystack = text if case_sensitive else text.lower()
    position = -1
    for token in tokens:
        needle = token if case_sensitive else token.lower()
        found = haystack.find(needle)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        position = 0
    start = max(0, position - width // 2)
    end = min(len(text), start + width)
    start = max(0, end - width)
    result = text[start:end]
    if start > 0:
        result = "…" + result
    if end < len(text):
        result += "…"
    return result


def clip_hit_text(text, tokens, case_sensitive):
    """Keep a window around the earliest match; giant minified lines can be MBs."""
    if len(text) <= CONTENT_HIT_TEXT:
        return text
    haystack = text if case_sensitive else text.lower()
    position = -1
    for token in tokens:
        needle = token if case_sensitive else token.lower()
        found = haystack.find(needle)
        if found >= 0 and (position < 0 or found < position):
            position = found
    if position < 0:
        position = 0
    start = max(0, position - CONTENT_HIT_TEXT // 2)
    return text[start : start + CONTENT_HIT_TEXT]


def match_quality(text, tokens, case_sensitive):
    flags = 0 if case_sensitive else re.IGNORECASE
    best = 0
    for token in tokens:
        escaped = re.escape(token)
        if re.search(r"(?<!\w)" + escaped + r"(?!\w)", text, flags):
            quality = 3
        elif re.search(r"(?<!\w)" + escaped, text, flags):
            quality = 2
        elif re.search(escaped, text, flags):
            quality = 1
        else:
            quality = 0
        if quality > best:
            best = quality
    return best


def content_item(rel, absolute, line, col, text, quality, tokens, case_sensitive):
    plain_title = snippet(text, tokens, case_sensitive)
    if not plain_title:
        plain_title = "(empty line)"
    title = "🔎 " + plain_title
    copy_text = "%s:%d" % (rel, line)
    variables = base_variables(line, col)
    return {
        "type": "file",
        "title": title,
        "subtitle": "%s:%d" % (rel, line),
        "arg": absolute,
        "quicklookurl": absolute,
        "text": {"copy": copy_text, "largetype": plain_title},
        "variables": variables,
        "mods": with_mods(variables),
    }


def indexing_payload(root):
    return {
        "rerun": 0.5,
        "skipknowledge": True,
        "items": [
            item(
                "Indexing %s…" % display_path(root),
                "Building the search index once; results will appear automatically",
                valid=False,
            )
        ],
    }


def empty_payload(root):
    meta = read_meta()
    bits = ["Type to fuzzy-match file and folder names; content matches follow"]
    if meta and meta.get("root") == root:
        bits.append("%s files, %s folders indexed" % (format(meta.get("files", 0), ","), format(meta.get("dirs", 0), ",")))

    mode = excludes_mode()
    if mode == "asimeow":
        config = asimeow_config_path()
        exclusion_source = display_path(str(config)) if config.is_file() else "built-in list (asimeow config not found)"
    elif mode == "builtin":
        exclusion_source = "built-in list"
    else:
        exclusion_source = "disabled"

    return {
        "skipknowledge": True,
        "items": [
            item(
                "Search %s" % display_path(root),
                " · ".join(bits),
                valid=False,
            ),
            item(
                "Tip",
                "⌥ reveal in Finder · ⌘ copy path · ⇥ file actions",
                valid=False,
            ),
            item("Exclusions", exclusion_source, valid=False),
        ],
    }


# --------------------------------------------------------------------------- #
# Content search (runs in a detached background process)
# --------------------------------------------------------------------------- #

def content_cache_dir():
    return CACHE / "content"


def content_hash(root, query):
    return hashlib.sha1((root + "\0" + query).encode("utf-8")).hexdigest()


def content_result_path(query_hash):
    return content_cache_dir() / (query_hash + ".json")


def content_request_path(query_hash):
    return content_cache_dir() / (query_hash + ".request.json")


def content_pid_path():
    return content_cache_dir() / "scan.pid"


def content_pattern(tokens):
    if len(tokens) == 1:
        return tokens, True
    pattern = "".join("(?=.*" + re.escape(token) + ")" for token in tokens) + ".*"
    return [pattern], False


def parse_content_output(out, root, tokens, max_lines):
    case_sensitive = smart_case(tokens)
    cache_real = real_cache_dir()
    hits = []
    seen = set()
    per_file = {}
    for raw_line in out.decode("utf-8", "replace").splitlines():
        parts = raw_line.split("\x1f", 3)
        if len(parts) != 4:
            continue
        path, line_raw, col_raw, text = parts
        try:
            line = int(line_raw)
            col = int(col_raw)
        except ValueError:
            continue
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        if under(path, cache_real):
            continue
        rel = nfc(os.path.relpath(path, root).replace(os.sep, "/"))
        if rel == ".." or rel.startswith("../"):
            continue
        if per_file.get(rel, 0) >= max_lines:
            continue
        key = (rel, line)
        if key in seen:
            continue
        seen.add(key)
        per_file[rel] = per_file.get(rel, 0) + 1
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            mtime = 0.0
        quality = match_quality(text, tokens, case_sensitive)
        hits.append(
            {
                "rel": rel,
                "line": line,
                "col": col,
                "text": clip_hit_text(text, tokens, case_sensitive),
                "quality": quality,
                "mtime": mtime,
            }
        )
        if len(hits) >= CONTENT_PARSE_HITS:
            break
    hits.sort(key=lambda hit: (-hit["quality"], -hit["mtime"], len(hit["rel"]), hit["rel"], hit["line"]))
    return hits


def active_scan():
    """Return (pid, query_hash, age) for a live scan supervisor, else None."""
    path = content_pid_path()
    try:
        raw = path.read_text(encoding="utf-8").split()
        pid = int(raw[0])
        active_hash = raw[1] if len(raw) > 1 else ""
        age = max(0.0, time.time() - path.stat().st_mtime)
    except (OSError, ValueError, IndexError):
        return None
    if not _pid_alive(pid):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return pid, active_hash, age


def clean_content_cache():
    now = time.time()
    try:
        entries = list(content_cache_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name == "scan.pid":
            continue
        try:
            if now - entry.stat().st_mtime > 3600:
                entry.unlink()
        except OSError:
            pass


def spawn_content_scan(query_hash, request):
    try:
        content_cache_dir().mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    active = active_scan()
    if active:
        pid, active_hash, age = active
        if active_hash == query_hash and age < CONTENT_SCAN_STALE:
            # The same query is already being scanned; replacing it would only
            # restart the clock. The caller reports pending so the UI reruns.
            return True
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    clean_content_cache()
    try:
        content_request_path(query_hash).write_text(json.dumps(request), encoding="utf-8")
    except OSError:
        return False
    try:
        subprocess.Popen(
            [sys.executable, str(HERE / "search.py"), "--content-scan", query_hash],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    except Exception:
        try:
            content_request_path(query_hash).unlink()
        except OSError:
            pass
        return False
    return True


def content_scan_main(query_hash):
    try:
        request = json.loads(content_request_path(query_hash).read_text(encoding="utf-8"))
    except Exception:
        return 0
    try:
        content_cache_dir().mkdir(parents=True, exist_ok=True)
        content_pid_path().write_text("%d %s" % (os.getpid(), query_hash), encoding="utf-8")
    except OSError:
        pass

    result = {
        "query": request.get("query", ""),
        "root": request.get("root", ""),
        "done": True,
        "built": time.time(),
        "hits": [],
        "error": "",
    }
    try:
        rg = find_tool("rg")
        if not rg:
            raise RuntimeError("ripgrep is not installed")
        root = request["root"]
        tokens = request["tokens"]
        max_lines = int(request.get("max_lines", 3))
        pattern_tokens, fixed = content_pattern(tokens)
        argv = content_args(rg, root, pattern_tokens, request.get("globs", []), max_lines, fixed)
        out = run_scan_capture(argv, CONTENT_SCAN_TIMEOUT, CONTENT_CAPTURE_BYTES)
        hits = parse_content_output(out, root, tokens, max_lines)
        result["hits"] = [
            {
                "rel": hit["rel"],
                "line": hit["line"],
                "col": hit["col"],
                "text": hit["text"],
                "quality": hit["quality"],
                "mtime": hit["mtime"],
            }
            for hit in hits[: CONTENT_LIMIT * 3]
        ]
    except Exception as exc:
        result["error"] = str(exc)
        _debug("content scan failed: %s" % traceback.format_exc())

    try:
        write_json_atomic(content_result_path(query_hash), result)
    except Exception:
        _debug("could not write content results: %s" % traceback.format_exc())
    try:
        current = content_pid_path().read_text(encoding="utf-8").strip()
        if current == "%d %s" % (os.getpid(), query_hash):
            content_pid_path().unlink()
    except OSError:
        pass
    return 0


def content_cache_state(query_hash, root, query, tokens, globs, max_lines, rg):
    """Return (hits, pending); hits is a list of cached hit dicts."""
    result_path = content_result_path(query_hash)
    result = None
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        result = None

    if result and result.get("done") and result.get("query") == query and result.get("root") == root:
        try:
            age = time.time() - float(result.get("built") or 0)
        except (TypeError, ValueError):
            age = 0
        if age > CONTENT_CACHE_TTL and rg:
            pending = spawn_content_scan(
                query_hash,
                {"query": query, "root": root, "tokens": tokens, "globs": globs, "max_lines": max_lines},
            )
            return result.get("hits") or [], bool(pending)
        return result.get("hits") or [], False

    request_path = content_request_path(query_hash)
    try:
        request_age = time.time() - request_path.stat().st_mtime
    except OSError:
        request_age = None
    if request_age is not None and request_age < 6:
        return [], True
    if not rg:
        return [], False
    request = {"query": query, "root": root, "tokens": tokens, "globs": globs, "max_lines": max_lines}
    if spawn_content_scan(query_hash, request):
        return [], True
    return [], False


def main():
    started = time.monotonic()
    if "--rebuild" in sys.argv[1:]:
        return rebuild_main()
    if len(sys.argv) > 1 and sys.argv[1] == "--content-scan":
        return content_scan_main(sys.argv[2] if len(sys.argv) > 2 else "")

    query = (sys.argv[1] if len(sys.argv) > 1 else "").strip()
    root = search_root()

    if not os.path.isdir(root):
        error_emit("Search folder not found", "%s — set search_root in this workflow's configuration" % display_path(root))
        return 0

    fzf = find_tool("fzf")
    rg = find_tool("rg")

    if not fzf:
        items = [
            item("fzf is required", "Install it with: brew install fzf", valid=False),
        ]
        if not rg:
            items.append(item("ripgrep is required", "Install it with: brew install ripgrep", valid=False))
        emit({"skipknowledge": True, "items": items})
        return 0

    if not query:
        emit(empty_payload(root))
        return 0

    names, extras = exclude_config()
    globs = exclude_globs(names, extras)
    max_items = _int_env("max_items", 30, 1, 100)
    include_dirs = _bool_env("include_dirs", True)
    content_enabled = _bool_env("content_enabled", True)
    max_lines = _int_env("max_content_lines", 3, 1, 10)

    meta = read_meta()
    _debug("query=%r root=%s state=%s" % (query, root, index_state(root, names, extras)))

    source = None
    live_bytes = None
    state = index_state(root, names, extras)
    if state == "missing":
        if lock_is_held():
            emit(indexing_payload(root))
            return 0
        if spawn_rebuild():
            emit(indexing_payload(root))
            return 0
        listing, listing_error = live_listing(root, names, extras)
        if listing is None:
            error_emit("Could not read %s" % display_path(root), listing_error or "Unknown error")
            return 0
        live_bytes = ("\n".join(listing) + "\n").encode("utf-8")
        source = live_bytes
    else:
        if state == "stale":
            spawn_rebuild()
        try:
            source = open(str(paths_file()), "rb")
        except OSError:
            source = None
        if source is None:
            error_emit("Index unavailable", "Delete the workflow data folder to rebuild")

    tokens = content_tokens(query)

    # Kick off (or fetch) the background content scan before the fast name search.
    cached_content = []
    content_pending = False
    if content_enabled and rg and tokens:
        cached_content, content_pending = content_cache_state(
            content_hash(root, query),
            root,
            query,
            tokens,
            globs,
            max_lines,
            rg,
        )

    try:
        name_hits = fzf_results(fzf, query, source, min(NAME_LIMIT, max_items) * 2)
    finally:
        if hasattr(source, "close"):
            source.close()

    name_items = []
    cache_real = real_cache_dir()
    for rel in name_hits:
        absolute = actual_path(root, rel)
        if under(absolute, cache_real):
            continue
        is_dir = os.path.isdir(absolute)
        if is_dir and not include_dirs:
            continue
        name_items.append(path_item(rel, absolute, is_dir, root))
        if len(name_items) >= min(NAME_LIMIT, max_items):
            break

    content_items = []
    content_limit = min(CONTENT_LIMIT, max(0, max_items - len(name_items)))
    if content_limit > 0 and cached_content:
        case_sensitive = smart_case(tokens)
        for hit in cached_content[:content_limit]:
            rel = str(hit.get("rel") or "")
            if not rel:
                continue
            absolute = actual_path(root, rel)
            if under(absolute, cache_real):
                continue
            content_items.append(
                content_item(
                    rel,
                    absolute,
                    int(hit.get("line") or 0),
                    int(hit.get("col") or 0),
                    str(hit.get("text") or ""),
                    int(hit.get("quality") or 0),
                    tokens,
                    case_sensitive,
                )
            )

    items = name_items + content_items
    if not rg:
        items.append(item("ripgrep is not installed", "Content search and index updates need it: brew install ripgrep", valid=False))

    if not items:
        if content_pending:
            items = [
                item(
                    "Searching file contents…",
                    "Name matches appear first; content results are still scanning",
                    valid=False,
                )
            ]
        else:
            items = [
                item(
                    "No matches for “%s”" % query,
                    "Try fewer words, or a different spelling",
                    valid=False,
                )
            ]

    more_names = len(name_hits) > len(name_items)
    more_content = len(cached_content) > len(content_items)
    if not content_pending and len(items) >= max_items and (more_names or more_content):
        items = items[: max_items - 1] + [
            item(
                "Showing top %d" % max_items,
                "Type more to narrow the search",
                valid=False,
            )
        ]

    _debug(
        "returned %d items in %.0f ms (content %s)"
        % (len(items), (time.monotonic() - started) * 1000, "pending" if content_pending else "ready")
    )
    payload = {"skipknowledge": True, "items": items[:max_items]}
    if content_pending:
        payload["rerun"] = 0.35
    emit(payload)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
    except Exception:
        _debug(traceback.format_exc())
        error_emit("Grep Files hit an internal error", "Set AGF_DEBUG=1 and check the workflow debugger")
        sys.exit(0)
