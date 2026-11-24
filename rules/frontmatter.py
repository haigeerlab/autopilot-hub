"""Plugin standard C7, second half: the frontmatter of every skill, command and agent really parses (card).

Spec: docs/plugin-standard.md (C7). `claude plugin validate --strict` does not check that frontmatter YAML parses
(kb-lead, 2.1.321 and 2.1.328), so this reads it with the hub's strict YAML subset parser (stdlib only; it rejects
anything it does not fully understand). skills/*/SKILL.md and agents/*.md need non-empty `name` and `description`;
commands/*.md need only `description` (a command's name comes from its file name).
Exit codes: 0 pass, 1 violation, 2 bad input.

    python3 -B rules/frontmatter.py --repo <checkout> [--out-md out.md]
"""
import argparse
import os
import re
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkup import yamlsub

RULE = "frontmatter"
SPEC = "docs/plugin-standard.md（C7）"
SKIP_DIRS = {".git", ".claude", ".hub", "node_modules", ".venv", "venv", "__pycache__"}  # .hub: the CI checkout of this repo
KINDS = (("skill", re.compile(r"(?:^|/)skills/[^/]+/SKILL\.md$"), ("name", "description")),
         ("agent", re.compile(r"(?:^|/)agents/[^/]+\.md$"), ("name", "description")),
         ("command", re.compile(r"(?:^|/)commands/[^/]+\.md$"), ("description",)))
FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)


def kind_of(rel):
    if rel.startswith("docs/") or "/docs/" in rel:
        return None
    for kind, rx, required in KINDS:
        if rx.search(rel):
            return kind, required
    return None


def files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            found = kind_of(rel)
            if found:
                yield rel, found[0], found[1]


def check(root):
    errors = []
    for rel, kind, required in files(root):
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as e:
            errors.append(f"{rel}：读不了（{e}）")
            continue
        m = FRONT.match(text)
        if not m:
            errors.append(f"{rel}：开头没有 `---` 包起来的元信息（{kind}）")
            continue
        try:
            data = yamlsub.loads(m.group(1))
        except yamlsub.YamlSubsetError as e:
            errors.append(f"{rel}：元信息的 YAML 解析不了（{e}）")
            continue
        if not isinstance(data, dict):
            errors.append(f"{rel}：元信息不是键值对")
            continue
        missing = [k for k in required if not (isinstance(data.get(k), str) and data[k].strip())]
        if missing:
            errors.append(f"{rel}：{kind} 的元信息缺少或为空：{'、'.join(missing)}")
    return errors


def render(errors):
    if not errors:
        return f"{RULE}: 通过"
    return "\n".join([f"{RULE}: {len(errors)} 个文件的元信息有问题"] + [f"- {e}" for e in errors] + [
        "为什么：元信息缺字段或 YAML 写错，本地不报错，用户装上才失败；官方校验命令不检查 YAML 能否解析。",
        "改成：文件开头用 `---` 包住元信息；skill、agent 写 name 和 description，command 写 description。",
        "不要：在单行值里写没闭合的 `[`、`{`，或把长描述折成多行而不用 `>`。",
        f"详见：{SPEC}"])


def main(argv=None):
    p = argparse.ArgumentParser(description="插件开发规范 C7：skill、command、agent 元信息校验")
    p.add_argument("--repo", required=True)
    p.add_argument("--out-md")
    a = p.parse_args(argv)
    if not os.path.isdir(a.repo):
        print(f"{RULE}: {a.repo} 不是目录", file=sys.stderr)
        return 2
    errors = check(a.repo)
    text = render(errors)
    print(text)
    if a.out_md:
        with open(a.out_md, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
