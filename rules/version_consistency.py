"""Plugin standard B1: every version declaration of a plugin is the same string (card).

Spec: docs/plugin-standard.md (B1). Declarations: `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`
(`version`, grouped by their `name`) and the entries of `.claude-plugin/marketplace.json` (`plugins[].version`,
by `name`). A place that is absent does not take part; the same name twice in one marketplace fails. Versions are
compared as raw strings. With --changed-files only runs when the change touched a declaration (or adds one).
Exit codes: 0 pass, 1 violation, 2 bad input.

    python3 -B rules/version_consistency.py --repo <checkout> [--changed-files files.json] [--out-md out.md]
"""
import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RULE = "version-consistency"
SPEC = "docs/plugin-standard.md（B1）"
MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
MARKETPLACE = ".claude-plugin/marketplace.json"
SKIP_DIRS = {".git", ".claude", "node_modules", ".venv", "venv", "__pycache__"}  # .claude: local worktrees


def is_declaration(path):
    path = path.replace(os.sep, "/")
    return any(path == m or path.endswith("/" + m) for m in MANIFESTS + (MARKETPLACE,))


def _files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            if is_declaration(rel):
                yield rel


def collect(root):
    """({plugin name: [(where, version)]}, [problems]) for every declaration under root."""
    found, problems = {}, []
    for rel in _files(root):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as e:
            problems.append(f"{rel} 读不懂：{e}")
            continue
        if not isinstance(data, dict):
            problems.append(f"{rel} 读不懂：不是 JSON 对象")
            continue
        if rel.endswith(MARKETPLACE) and not rel.endswith("plugin.json"):
            seen = {}
            for entry in data.get("plugins") or []:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                    continue
                seen[entry["name"]] = seen.get(entry["name"], 0) + 1
                if "version" in entry:
                    found.setdefault(entry["name"], []).append((f"{rel}（{entry['name']} 条目）", entry["version"]))
            problems += [f"{rel} 里插件 {n} 出现 {c} 次" for n, c in sorted(seen.items()) if c > 1]
        elif isinstance(data.get("name"), str) and "version" in data:
            found.setdefault(data["name"], []).append((rel, data["version"]))
    return found, problems


def versions_by_plugin(root):
    """{plugin name: version} for plugins whose declarations agree (B5 reads this)."""
    found, _ = collect(root)
    return {name: places[0][1] for name, places in found.items() if len({str(v) for _, v in places}) == 1}


def check(root, changed=None):
    if changed is not None and not any(is_declaration(p) for p in changed):
        return []
    found, problems = collect(root)
    errors = list(problems)
    for name, places in sorted(found.items()):
        if len({json.dumps(v) for _, v in places}) > 1:
            errors.append(f"插件 {name} 的版本号不一致：" + "；".join(f"{w} = {v}" for w, v in places))
    return errors


def render(errors):
    if not errors:
        return f"{RULE}: 通过"
    return "\n".join([f"{RULE}: {len(errors)} 个问题"] + [f"- {e}" for e in errors] + [
        "为什么：版本号不一致时，Claude 和 Codex 装到的不是同一个版本，排查时还会误以为是同一份。",
        "改成：把这个插件所有已存在的版本声明改成同一个字符串（发版命令的 prepare 会一起改）。",
        f"详见：{SPEC}"])


def main(argv=None):
    p = argparse.ArgumentParser(description="插件开发规范 B1：版本号各处一致")
    p.add_argument("--repo", required=True)
    p.add_argument("--changed-files", help="PR 文件列表 JSON（只看 filename）；不给就检查全部")
    p.add_argument("--out-md")
    a = p.parse_args(argv)
    try:
        if not os.path.isdir(a.repo):
            raise OSError(f"{a.repo} 不是目录")
        changed = None
        if a.changed_files:
            with open(a.changed_files, encoding="utf-8") as fh:
                changed = [f.get("filename") or "" for f in json.load(fh)]
    except (OSError, ValueError, AttributeError) as e:
        print(f"{RULE}: 输入读不了：{e}", file=sys.stderr)
        return 2
    errors = check(a.repo, changed)
    text = render(errors) if changed is None or errors or any(is_declaration(p) for p in changed) \
        else f"{RULE}: 通过（本次没有改到版本声明）"
    print(text)
    if a.out_md:
        with open(a.out_md, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
