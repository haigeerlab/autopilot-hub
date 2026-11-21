"""Command line for slice-4 risk tiers.

  paths    --repo <local checkout> [--json]   T3/T4 path lists for one repo (read-only interface for spec-guard)
  classify --files <pr-files.json> [--config <risk-tiers.json>]   tier of a PR's changed files (JSON out)

Exit codes: 0 ok; 2 bad input (unreadable files or config).
"""
import argparse
import json
import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk import tiers  # noqa: E402

CONFIG_PATH = os.path.join(".github", "autopr", "risk-tiers.json")


def main(argv=None):
    p = argparse.ArgumentParser(description="autopilot risk tiers")
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("paths")
    pp.add_argument("--repo", required=True)
    pp.add_argument("--json", action="store_true")
    pc = sub.add_parser("classify")
    pc.add_argument("--files", required=True)
    pc.add_argument("--config")
    a = p.parse_args(argv)
    templates = tiers.load_templates()
    try:
        if a.cmd == "paths":
            config = tiers.read_repo_config(os.path.join(a.repo, CONFIG_PATH))
            out = tiers.risk_paths(templates, config)
        else:
            with open(a.files, encoding="utf-8") as fh:
                files = json.load(fh)
            out = tiers.classify(files, templates, tiers.read_repo_config(a.config))
    except (OSError, ValueError) as e:
        print(f"risk: {e}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1 if a.cmd == "classify" or getattr(a, "json", False) else None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
