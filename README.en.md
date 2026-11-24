# autopilot-hub

[中文](README.md)

The central repository of autopilot, haigeerlab's automated maintenance system. The admission, merge, degrade, risk, rule-check, checkup, release and compliance logic shared by every plugin repository lives here; each repository references it by a 40-character commit SHA.

Development standard for the plugin repositories: [docs/plugin-standard.md](docs/plugin-standard.md) (in Chinese).

## Status

- **Slice 1** (retired, see slice 7): admission used to read repo-readiness conclusions and compute a trust ceiling. Spec kept for history: [docs/spec/slice-1.md](docs/spec/slice-1.md).
- **Slice 2**: opens an `autopilot:degrade` issue when something goes wrong (an autopilot PR is reverted, main turns red after an autopilot merge, or `autopilot:regression` / `autopilot:freeze` is added). While any degrade issue is open, admission reports "will not auto-merge". [Spec](docs/spec/slice-2.md).
- **Slice 3** (retired, see slice 7): a weekly local readiness recheck. [Spec](docs/spec/slice-3.md).
- **Slice 4**: risk tiers T0–T4 for every PR (shared rules + four templates + repository additions, which may only tighten), with reasons and whether it may auto-merge (T0–T2 yes, T3 needs a human merge, T4 stops for a human). `python3 -B risk/cli.py paths --repo <checkout> --json` gives spec-guard the high-risk paths. [Spec](docs/spec/slice-4.md).
- **Slice 5**: checkup. `python3 -B checkup/cli.py --repo <local checkout> [--base main] [--run-check] [--out <dir>] [--json]` runs 14 checks (C1–C14) on a local checkout and only reports; fixes are generated as patches and commands, never applied automatically. [Spec](docs/spec/slice-5.md).
- **Slice 6**: correction records. `correction/cli.py` records a correction as an issue per class and asks on the second occurrence whether to turn it into a rule. Three rule checks in `rules/` (no weakened assertions, a shrinking suppression budget, Chinese commit messages) run through the reusable workflow `.github/workflows/rules.yml`. [Spec](docs/spec/slice-6.md).
- **Slice 7**: admission reports a single verdict, "can this auto-merge", from four items: risk tier, the takeover comment, the hard gate, and open degrade issues. It still only reports (`neutral`). [Spec](docs/spec/slice-7.md).
- **Slice 8**: admission trusts takeover comments only from the autopilot App bot listed in `bot-identity.json`, and only if they were never edited. [Spec](docs/spec/slice-8.md).
- **Slice 9**: the hard gate reads the bot's `autopilot-evidence:v1` comment bound to the current head SHA: all checks passed, review completed with no blocking findings, plus final acceptance for spec-guard modules. [Spec](docs/spec/slice-9.md).
- **Slice 10**: local orchestrator `orchestrator/cli.py run --repo <owner/name> --pr <n> [--dry-run]`: posts the takeover comment as the bot, runs checks in a throwaway clone, runs the delegate review, posts evidence, recomputes admission with the same code, then merges the reviewed head SHA and reads it back. [Spec](docs/spec/slice-10.md).
- **Slice 11**: narrowing and counts. Degrade issues from reverts, red main or regressions open a narrowing PR through spec-guard's `narrow` (T4); only an admin's close lifts a degrade issue; `orchestrator/cli.py stats` reports three counts. [Spec](docs/spec/slice-11.md).
- **Slice 12**: a spec-guard module's own `spec/<module>.md` and `tasks/<module>/**` drop from T4 to T2 once the orchestrator verifies on head that their approvals still hold. [Spec](docs/spec/slice-12.md).
- **Slice 13**: pipeline file. `.github/autopr/pipeline.yml` only states differences from a hub template; `python3 -B pipeline/cli.py show --repo <checkout> [--json]` validates and prints the merged result. [Spec](docs/spec/slice-13.md).
- **Slice 14**: release engine. `orchestrator/cli.py release --repo <owner/name> --version <v> [--dry-run]` runs the release segments with a bot tracking issue, stopping for confirmation or failure; `--resume <issue> [--confirm "the user's words"]` continues. [Spec](docs/spec/slice-14.md).
- **Slice 15**: release caps from `release-limits.json`, enforced by the core whatever a repository asks for. [Spec](docs/spec/slice-15.md).
- **Slice 16**: plugin, npm and site release templates, the `pipeline/entry.py` entry runner, checkup C14, and pre-merge pipeline steps. [Spec](docs/spec/slice-16.md).
- **Slice 18**: compliance audit. `audit/baselines.json` records each repository's approved takeover levels, pins and checkup state (T4 to change). `python3 -B audit/cli.py` checks takeover widening, pin changes, checkup regressions, open degrade issues and Plan approvals (including review rounds), writes a weekly report issue on the hub and one `autopilot:audit` issue per repository and kind of violation, and never closes them automatically. [Spec](docs/spec/slice-18.md).
- **module-crew first-stage merges**: `orchestrator/cli.py batch --repo <owner/name> [--dry-run]`. [Spec](docs/spec/module-crew-batch.md).
- Everything else is still in design; design notes live in the plugin-ecosystem ledger.

## Rules

- **Everything in this repository is T4**: never auto-merged; every change is reviewed and merged by a human, because these rules and scripts are the referee.
- Other repositories pin this one by 40-character SHA, never by branch or tag. Changing that pin is T4 in those repositories too.
- Scripts use only the Python standard library and must run on macOS `/usr/bin/python3` (3.9) and on Ubuntu.

## License

[MIT](LICENSE)
