"""Plugin standard B5: a release tag is `v<B1 version>`, annotated, on the remote default branch (card).

Spec: docs/plugin-standard.md (B5). Called by release commands before they create the tag, and by the
tag-push workflow after a tag is pushed (then a failure means: delete the tag and tag again).
Fetches the remote first unless --no-fetch. Exit codes: 0 pass, 1 violation, 2 bad input or git failure.

    python3 -B rules/release_tag.py --repo <checkout> --tag v1.2.0 [--plugin <name>] [--remote origin]
        [--default-branch main] [--no-fetch] [--out-md out.md]
"""
import argparse
import os
import subprocess
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rules.version_consistency import check as b1_check, is_declaration, versions_by_plugin

RULE = "release-tag"
SPEC = "docs/plugin-standard.md（B5）"


class GitError(Exception):
    pass


def _git(repo, *args):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout.strip(), r.stderr.strip()


def default_branch(repo, remote):
    code, out, _ = _git(repo, "symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD")
    if code == 0 and out.startswith(remote + "/"):
        return out[len(remote) + 1:]
    code, out, _ = _git(repo, "ls-remote", "--symref", remote, "HEAD")
    for line in out.splitlines():
        if line.startswith("ref: refs/heads/"):
            return line.split()[1][len("refs/heads/"):]
    raise GitError(f"判断不出 {remote} 的默认分支，请用 --default-branch 指定")


def declarations_at(repo, rev, dest):
    """Write the version declarations of commit `rev` under dest (the working tree may be ahead of the tag)."""
    code, out, err = _git(repo, "ls-tree", "-r", "--name-only", rev)
    if code != 0:
        raise GitError(f"列不出 {rev} 的文件：{err[:200]}")
    for rel in out.splitlines():
        if not is_declaration(rel):
            continue
        code, text, err = _git(repo, "show", f"{rev}:{rel}")
        if code != 0:
            raise GitError(f"读不了 {rev}:{rel}：{err[:200]}")
        path = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)


def check(repo, tag, plugin=None, remote="origin", branch=None, fetch=True):
    errors = []
    code, kind, _ = _git(repo, "cat-file", "-t", f"refs/tags/{tag}")
    if code != 0:
        return [f"找不到标签 {tag}"]
    with tempfile.TemporaryDirectory(prefix="release-tag-") as tree:
        declarations_at(repo, f"refs/tags/{tag}^{{commit}}", tree)
        versions, b1_errors = versions_by_plugin(tree), b1_check(tree)
    if b1_errors:
        errors.append("标签指向的提交里版本号各处不一致（先过 B1），标签名无从比对")
    elif plugin is None and len(versions) != 1:
        errors.append("仓库里有 %d 个插件，要用 --plugin 指定是哪一个" % len(versions))
    else:
        name = plugin or next(iter(versions))
        if name not in versions:
            errors.append(f"找不到插件 {name} 的版本声明")
        elif tag != "v" + str(versions[name]):
            errors.append(f"标签名是 {tag}，应为 v{versions[name]}（插件 {name} 在该提交里的版本号）")
    if kind != "tag":
        errors.append(f"{tag} 是轻量标签，必须是附注标签（git tag -a）")
    if fetch:
        code, _, err = _git(repo, "fetch", "-q", remote)
        if code != 0:
            raise GitError(f"fetch {remote} 失败：{err[:200]}")
    branch = branch or default_branch(repo, remote)
    code, commit, err = _git(repo, "rev-parse", f"{tag}^{{commit}}")
    if code != 0:
        raise GitError(f"解析不了 {tag} 指向的提交：{err[:200]}")
    code, _, _ = _git(repo, "merge-base", "--is-ancestor", commit, f"{remote}/{branch}")
    if code == 1:
        errors.append(f"{tag} 指向的提交 {commit[:12]} 不在 {remote} 的默认分支 {branch} 上")
    elif code != 0:
        raise GitError(f"核对不了 {commit[:12]} 是否在 {remote}/{branch} 上")
    return errors


def render(tag, errors):
    if not errors:
        return f"{RULE}: {tag} 通过"
    return "\n".join([f"{RULE}: {tag} 不合规"] + [f"- {e}" for e in errors] + [
        "为什么：轻量标签和打在未合并提交上的标签，会让「装上的是哪份代码」对不上。",
        "改成：在已合并进默认分支的发版提交上 `git tag -a v<版本> -m v<版本>`；已经推上去的，删掉远端标签后重打。",
        f"详见：{SPEC}"])


def main(argv=None):
    p = argparse.ArgumentParser(description="插件开发规范 B5：标签规则")
    p.add_argument("--repo", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--plugin")
    p.add_argument("--remote", default="origin")
    p.add_argument("--default-branch")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--out-md")
    a = p.parse_args(argv)
    try:
        errors = check(a.repo, a.tag, a.plugin, a.remote, a.default_branch, not a.no_fetch)
    except GitError as e:
        print(f"{RULE}: {e}", file=sys.stderr)
        return 2
    text = render(a.tag, errors)
    print(text)
    if a.out_md:
        with open(a.out_md, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
