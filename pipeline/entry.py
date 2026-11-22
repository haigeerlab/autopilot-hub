"""Run a repository's registered interface entry from its release checkout."""
import argparse
import os
import re
import subprocess
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from checkup import yamlsub


def entry_name(value):
    if not re.fullmatch(r"[a-z0-9_]+", value):
        raise argparse.ArgumentTypeError("入口名只允许小写字母、数字和下划线（[a-z0-9_]+）")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="执行当前仓库 interface.yml 登记的入口")
    parser.add_argument("entry", type=entry_name, help="入口名")
    name = parser.parse_args(argv).entry
    try:
        config = yamlsub.load(".github/autopr/interface.yml")
        entries = config.get("entries") if isinstance(config, dict) else None
        entry = entries.get(name) if isinstance(entries, dict) else None
        command = entry.get("run") if isinstance(entry, dict) else None
    except (OSError, ValueError):
        command = None
    if not isinstance(command, str) or not command.strip():
        print("interface.yml 没登记 entries.%s.run，这一步无法自动做，请在 pipeline.yml 里 replace 成自己的命令或 remove"
              % name, file=sys.stderr)
        return 3
    return subprocess.run(["/bin/sh", "-c", command], stdin=subprocess.DEVNULL).returncode


if __name__ == "__main__":
    sys.exit(main())
