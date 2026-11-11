#!/usr/bin/env python3
"""Grep Files: action dispatcher.

Connected to the Script Filter's Run Script action. Alfred passes the selected
item's `arg` as argv[1] and exports the item's variables (`action`, `line`,
`col`) as environment variables.

Actions:
  open    file -> default app; directory -> reveal in Finder;
          content hit (line > 0) -> editor at line, else default app
  reveal  reveal the file/folder in Finder
  copy    copy the absolute path, or path:line for a content hit
"""

import os
import shlex
import subprocess
import sys

BIN_DIRS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/usr/bin",
    "/bin",
]

EDITORS = (
    # name, argv template
    ("code", ["code", "-g", "{path}:{line}:{col}"]),
    ("cursor", ["cursor", "-g", "{path}:{line}:{col}"]),
    ("zed", ["zed", "{path}:{line}:{col}"]),
    ("subl", ["subl", "{path}:{line}:{col}"]),
)


def find_tool(name):
    seen = set()
    for directory in os.environ.get("PATH", "").split(os.pathsep) + BIN_DIRS:
        if not directory or directory in seen:
            continue
        seen.add(directory)
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def run_detached(argv):
    """Launch and return; actions should not make Alfred wait."""
    subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def copy_to_clipboard(text):
    subprocess.run(
        ["pbcopy"],
        input=text.encode("utf-8"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def expand_template(template, path, line, col):
    try:
        tokens = shlex.split(template)
    except ValueError:
        tokens = [template]
    values = {"{path}": path, "{line}": str(line), "{col}": str(col)}
    expanded = []
    for token in tokens:
        for placeholder, value in values.items():
            token = token.replace(placeholder, value)
        expanded.append(token)
    return expanded


def editor_argv(path, line, col):
    template = (os.environ.get("editor_command") or "").strip()
    if template:
        argv = expand_template(template, path, line, col)
        if argv:
            executable = find_tool(argv[0]) if os.sep not in argv[0] else argv[0]
            if executable:
                argv[0] = executable
                return argv
        return None

    for name, candidate in EDITORS:
        executable = find_tool(name)
        if executable:
            argv = list(candidate)
            argv[0] = executable
            return expand_template_safe(argv, path, line, col)
    return None


def expand_template_safe(argv, path, line, col):
    values = {"{path}": path, "{line}": str(line), "{col}": str(col)}
    expanded = []
    for token in argv:
        for placeholder, value in values.items():
            token = token.replace(placeholder, value)
        expanded.append(token)
    return expanded


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    if not path:
        sys.stderr.write("Grep Files: no path passed to the action\n")
        return 1

    action = (os.environ.get("action") or "open").strip().lower()
    try:
        line = int(os.environ.get("line") or 0)
    except ValueError:
        line = 0
    try:
        col = int(os.environ.get("col") or 0)
    except ValueError:
        col = 0

    if action == "copy":
        copy_to_clipboard("%s:%d" % (path, line) if line > 0 else path)
        return 0

    if action == "reveal":
        run_detached(["open", "-R", path])
        return 0

    # open
    if os.path.isdir(path):
        run_detached(["open", "-R", path])
        return 0

    if line > 0:
        argv = editor_argv(path, line, col)
        if argv:
            try:
                run_detached(argv)
                return 0
            except Exception as exc:
                sys.stderr.write("Grep Files: editor failed (%s), falling back to open\n" % exc)

    run_detached(["open", path])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        sys.stderr.write("Grep Files: action failed: %s\n" % exc)
        sys.exit(1)
