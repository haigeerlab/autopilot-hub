"""Added lines of a change, for the plugin-standard rules that only look at new or changed lines (A1, A2).

Two inputs: the PR file list from the GitHub API (`[{filename, status, patch}]`, collected by rules.yml), or a
local unified diff (`git diff`), so the same check runs in CI and before a local commit or push.
Stdlib only; supports Python 3.9.
"""
import ast
import json
import re

HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(f):
    """(line number in the new file, text) for every added line of one file's patch."""
    line = None
    for raw in (f.get("patch") or "").splitlines():
        m = HUNK.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if line is None or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            yield line, raw[1:]
            line += 1
        elif not raw.startswith("-"):
            line += 1


def _unquote(name):
    """Git C-quotes paths with special or non-ASCII characters; undo that when possible."""
    if len(name) >= 2 and name[0] == name[-1] == '"':
        try:
            value = ast.literal_eval(name)
            if isinstance(value, str):
                return value.encode("latin-1").decode("utf-8")
        except (ValueError, SyntaxError, UnicodeError, MemoryError, RecursionError):
            pass
        return name[1:-1]
    return name


def _binary_name(raw):
    """The new-side path of `Binary files A and B differ` (the old side when the file was deleted)."""
    rest = raw[len("Binary files "):-len(" differ")]
    if rest.endswith(" and /dev/null"):
        name, prefix = rest[:-len(" and /dev/null")], "a/"
    else:
        cut = max(rest.rfind(" and b/"), rest.rfind(' and "b/'))
        if cut < 0:
            return None
        name, prefix = rest[cut + len(" and "):], "b/"
    name = _unquote(name)
    return name[len(prefix):] if name.startswith(prefix) else name


def files_from_diff(text):
    """A local unified diff -> the same shape as the API file list (status added/modified/removed)."""
    files, cur, body = [], None, []

    def flush():
        if cur is not None:
            header_name, header_raw = cur.pop("header_name", None), cur.pop("header_raw")
            rename_to, binary_name = cur.pop("rename_to", None), cur.pop("binary_name", None)
            if cur.pop("binary", False):
                # Never drop a binary change: fall back to the raw header so it is still listed as unchecked.
                cur["filename"] = cur["filename"] or rename_to or header_name or binary_name or header_raw
                cur["patch"] = None  # like the API: binary content cannot be checked
            else:
                cur["patch"] = "\n".join(body)
            files.append(cur)

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            flush()
            cur, body = {"filename": None, "status": "modified", "patch": "", "header_raw": raw[len("diff --git "):]}, []
            # Binary diffs have no ---/+++ lines; take the name from the header when it is unambiguous.
            rest = raw[len("diff --git "):]
            half = (len(rest) - len("a/ b/")) // 2
            quoted = (len(rest) - len('"a/" "b/"')) // 2
            if half > 0 and rest == "a/" + rest[2:2 + half] + " b/" + rest[2:2 + half]:
                cur["header_name"] = rest[2:2 + half]
            elif quoted > 0 and rest == '"a/' + rest[3:3 + quoted] + '" "b/' + rest[3:3 + quoted] + '"':
                cur["header_name"] = _unquote('"' + rest[3:3 + quoted] + '"')
        elif cur is not None and raw.startswith("new file mode"):
            cur["status"] = "added"
        elif cur is not None and raw.startswith("deleted file mode"):
            cur["status"] = "removed"
        elif cur is not None and not body and raw.startswith("rename to "):
            cur["rename_to"] = _unquote(raw[len("rename to "):])
        elif cur is not None and not body and raw == "GIT binary patch":
            cur["binary"] = True
        elif cur is not None and not body and raw.startswith("Binary files ") and raw.endswith(" differ"):
            cur["binary"], cur["binary_name"] = True, _binary_name(raw)
        elif cur is not None and raw.startswith("--- ") and not body:
            continue
        elif cur is not None and raw.startswith("+++ ") and not body:
            name = raw[4:].strip()
            if name == "/dev/null":
                cur["status"] = "removed"
            else:
                cur["filename"] = name[2:] if name.startswith("b/") else name
        elif cur is not None and (body or raw.startswith("@@")):
            body.append(raw)
    flush()
    return [f for f in files if f["filename"]]


def load_files(pr_files=None, diff=None):
    """The file list from --pr-files (JSON path) or --diff (unified diff path, '-' for stdin)."""
    if pr_files:
        with open(pr_files, encoding="utf-8") as fh:
            files = json.load(fh)
        if not isinstance(files, list):
            raise ValueError("--pr-files 要是文件列表")
        return files
    import sys
    text = sys.stdin.read() if diff == "-" else open(diff, encoding="utf-8").read()
    return files_from_diff(text)


def unchecked(files):
    """Patchless changes cannot be checked, except newly added files with zero added lines."""
    return [f.get("filename") for f in files if f.get("status") != "removed" and f.get("patch") is None
            and not (f.get("status") == "added" and f.get("additions") == 0)]
