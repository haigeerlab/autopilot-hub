# autopilot-hub

[中文](README.md)

The central repository of autopilot, haigeerlab's automated maintenance system. The admission, merge, degrade, risk, rule-check, checkup, release and compliance logic shared by every plugin repository lives here; each repository references it by a 40-character commit SHA.

Development standard for the plugin repositories: [docs/plugin-standard.md](docs/plugin-standard.md) (in Chinese).

## Status

- **hub-auto-merge** uses `run` to write `autopilot/verified` on the commit assessed in pre-merge verification. A pass writes `success` and links to the evidence. A failure writes `failure` and disables queued auto-merge. New-flow modules with an `auto` merge setting take the `automerge` route when they pass the risk and admission checks and meet deployment requirements. The hub adds `autopilot:authored` first, then enables GitHub auto-merge for the verified commit. T4, legacy-flow and Dependabot PRs and other PRs ineligible for auto-merge take the `manual` route after passing verification. `mergeMode=direct` selects the original direct merge route. [Spec](spec/hub-auto-merge.md)
- **PRs without an assigned module** go through repository checks and risk grading in a regular `run`. They receive `autopilot/verified` once the repository checks pass; a failed risk check (T3, T4, `human_only`) still gets the status, with the reason recorded in the route reasons. They always take the `manual` route and are merged by you.
- **Monitoring and pauses** are handled by the local `watch` command. It disables queued auto-merge and pauses the PR if the commit changes, the verification status becomes invalid, or the wait times out. After a merge it adds `autopilot:auto-merged` and opens follow-up issues. Environment failures get up to two reruns, CI configuration errors pause for human intervention, and PR test failures go to the responsible session for analysis. When tests fail on the default branch of a repository monitored by watch, it first narrows the repository locally, then disables its queued auto-merges, opens a degrade issue, attempts to open a narrowing PR, and notifies the user. A human merges the narrowing PR. When default-branch CI passes, watch records the target commit and any verifiable PR and evidence links in `runs` in `~/.autopilot/watch-state.json` for the release module.
- **AI-operated cases** use delegate 0.13.2 or later. For a Claude Code author, the backend is Codex. Each `ai-operate` case from an approved module runs once in its own throwaway clone. The verification report records the result, evidence, the `authorization` object from delegate's result file, and the full authorization file SHA-256 computed by the hub. First record your exact words when you enabled the repository's automatic route with `python3 -B orchestrator/cli.py authorize --repo owner/name --quote "Your exact words" --session "Recording session"`. To revoke the authorization, run `python3 -B orchestrator/cli.py authorize --repo owner/name --revoke`. Cases fall back to manual execution when authorization is unavailable, the reported authorization fails validation, the author is Codex, the author host is unspecified, delegate is unavailable or asks for input, or its result cannot be parsed.
- **Slice 1** (retired, see slice 7): admission used to read repo-readiness conclusions and compute a trust ceiling. Spec kept for history: [docs/spec/slice-1.md](docs/spec/slice-1.md).
- **Slice 2**: opens an `autopilot:degrade` issue when something goes wrong (an autopilot PR is reverted, main turns red after an autopilot merge, or `autopilot:regression` / `autopilot:freeze` is added). While any degrade issue is open, admission reports "will not auto-merge". [Spec](docs/spec/slice-2.md).
- **Slice 3** (retired, see slice 7): a weekly local readiness recheck. [Spec](docs/spec/slice-3.md).
- **Slice 4**: risk tiers T0–T4 for every PR (shared rules + four templates + repository additions, which may only tighten), with reasons and whether it may auto-merge (T0–T2 yes, T3 needs a human merge, T4 stops for a human). `python3 -B risk/cli.py paths --repo <checkout> --json` gives spec-guard the high-risk paths. [Spec](docs/spec/slice-4.md).
- **Slice 5**: checkup. `python3 -B checkup/cli.py --repo <local checkout> [--base main] [--run-check] [--out <dir>] [--json]` runs 14 checks (C1–C14) on a local checkout and only reports; fixes are generated as patches and commands, never applied automatically. [Spec](docs/spec/slice-5.md).
- **Slice 6**: correction records. `correction/cli.py` records a correction as an issue per class and asks on the second occurrence whether to turn it into a rule. Three rule checks in `rules/` (no weakened assertions, a shrinking suppression budget, Chinese commit messages) run through the reusable workflow `.github/workflows/rules.yml`. [Spec](docs/spec/slice-6.md).
- **Slice 7**: admission reports a single verdict, "can this auto-merge", from four items: risk tier, the takeover comment, the hard gate, and open degrade issues. It still only reports (`neutral`). [Spec](docs/spec/slice-7.md).
- **Slice 8**: admission trusts takeover comments only from the autopilot App bot listed in `bot-identity.json`, and only if they were never edited. [Spec](docs/spec/slice-8.md).
- **Slice 9**: the hard gate reads the bot's `autopilot-evidence:v1` comment bound to the current head SHA: all checks passed, review completed with no blocking findings, plus final acceptance for spec-guard modules. [Spec](docs/spec/slice-9.md).
- **Slice 10** (original direct merge route): local orchestrator `orchestrator/cli.py run --repo <owner/name> --pr <n> [--dry-run]`: posts the takeover comment as the bot, runs checks in a throwaway clone, runs the delegate review, posts evidence, recomputes admission with the same code, then merges the reviewed head SHA and reads it back. [Spec](docs/spec/slice-10.md).
- **Slice 11**: narrowing and counts. Degrade issues from reverts, red main or regressions open a narrowing PR through spec-guard's `narrow` (T4); only an admin's close lifts a degrade issue; `orchestrator/cli.py stats` reports three counts. [Spec](docs/spec/slice-11.md).
- **Slice 12**: a spec-guard module's own `spec/<module>.md` and `tasks/<module>/**` drop from T4 to T2 once the orchestrator verifies on head that their approvals still hold. [Spec](docs/spec/slice-12.md).
- **Slice 13**: pipeline file. `.github/autopr/pipeline.yml` only states differences from a hub template; `python3 -B pipeline/cli.py show --repo <checkout> [--json]` validates and prints the merged result. [Spec](docs/spec/slice-13.md).
- **Slice 14**: release engine. `orchestrator/cli.py release --repo <owner/name> --version <v> [--dry-run]` runs the release segments with a bot tracking issue, stopping for confirmation or failure; `--resume <issue> [--confirm "the user's words"]` continues. [Spec](docs/spec/slice-14.md).
- **Slice 15**: release caps from `release-limits.json`, enforced by the core whatever a repository asks for. [Spec](docs/spec/slice-15.md).
- **Slice 16**: plugin, npm and site release templates, the `pipeline/entry.py` entry runner, checkup C14, and pre-merge pipeline steps. [Spec](docs/spec/slice-16.md).
- **Slice 18**: compliance audit. `audit/baselines.json` records each repository's approved takeover levels, pins and checkup state (T4 to change). `python3 -B audit/cli.py` checks takeover widening, pin changes, checkup regressions, open degrade issues and Plan approvals (including review rounds), writes a weekly report issue on the hub and one `autopilot:audit` issue per repository and kind of violation, and never closes them automatically. [Spec](docs/spec/slice-18.md).
- **module-crew first-stage merges**: `orchestrator/cli.py batch --repo <owner/name> [--dry-run]`. [Spec](docs/spec/module-crew-batch.md).
- Everything else is still in design; design notes live in the plugin-ecosystem ledger.

## Install and operate watch

Sign in to macOS as the user whose keychain is configured with the autopilot App credential. Copy the LaunchAgent template from the hub checkout root.

```sh
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs/autopilot"
plist="$HOME/Library/LaunchAgents/com.haigeerlab.autopilot-watch.plist"
cp examples/launchd/com.haigeerlab.autopilot-watch.plist "$plist"
```

Edit the copied plist. Replace every `__HUB_CHECKOUT__` with the absolute path to the hub checkout, every `__LOG_DIR__` with the absolute path to an existing writable log directory (such as the one created above), and `__GH_BIN_DIR__` with the directory containing the executable returned by `command -v gh`. Use actual paths in the plist because launchd does not expand `~` or `$HOME`. [LaunchAgent template](examples/launchd/com.haigeerlab.autopilot-watch.plist)

Validate and load the plist. launchd runs one round of `watch --once` every five minutes.

```sh
plutil -lint "$plist" && launchctl bootstrap gui/$UID "$plist"
```

To unload the agent, run the following command.

```sh
launchctl bootout gui/$UID "$HOME/Library/LaunchAgents/com.haigeerlab.autopilot-watch.plist"
```

To keep the agent unloaded at the next login, move the copied plist out of `~/Library/LaunchAgents`.

Run these commands from the hub checkout root, replacing the example repository and PR number with actual values.

- `python3 -B orchestrator/cli.py watch --freeze` sets the global freeze and runs one round to disable queued auto-merge. During the freeze, `run` on the default automatic route hands merges to a human, and watch only handles disabling queued auto-merge.
- `python3 -B orchestrator/cli.py watch --unfreeze` lifts the global freeze.
- `python3 -B orchestrator/cli.py watch --resume owner/name` lifts local narrowing in hub for the repository after the user confirms recovery.
- `python3 -B orchestrator/cli.py watch --resume 'owner/name#123'` clears the pause for that PR. Successful re-verification also clears the PR pause. Run `run` again to re-enable auto-merge.

After opening a PR, run `python3 -B orchestrator/cli.py run --repo owner/name --pr 123 --notify "session-name" --author-host claude-code`. Use `--notify` for the session that should receive handoff notices and `--author-host` for the actual author host, `claude-code` or `codex`. To record a pause and queue a user notification for a failure that watch cannot observe, run `python3 -B orchestrator/cli.py report-failure --repo owner/name --pr 0 --stage open-pr --detail "Reason the PR could not be opened"`. Use `0` when there is no PR number yet.

## Before enabling the automatic route

Check these items for each repository.

- [ ] To run `ai-operate` cases with AI, you have recorded your exact words with `cli.py authorize`.
- [ ] The hub GitHub App is installed on the repository. Its permissions include read and write access to Contents, Pull requests, Issues, Commit statuses and Actions, and read-only access to Checks.
- [ ] watch is installed and running. The `heartbeat` in `~/.autopilot/watch-state.json` is less than 15 minutes old.
- [ ] `repos.<repo>.watchMainRed` in `~/.autopilot/config.json` is `true`, where `<repo>` is the full `owner/name` key.
- [ ] The degrade caller's `uses:` and `hub_ref` are both pinned to the same 40-character hub commit that includes this module. Once watch is installed and `watchMainRed` is enabled, set `skip_main_red: true` under the caller's `with:` to hand default-branch CI failures to watch. [Degrade caller example](examples/caller-autopilot-degrade.yml)
- [ ] Update the hub pin to a commit that verifies PRs without an assigned module before requiring `autopilot/verified` on the default branch. Pin the check's source to the hub GitHub App and require PR branches to be up to date with the default branch.
- [ ] Exercise auto-merge and narrowing after a default-branch test failure in the sandbox first.

## Rules

- **Everything in this repository is T4**: the user decides every merge in the conversation (standard F3: after the user says "merge", the session runs `gh pr merge --auto`, and GitHub merges once `CI required` passes); "merge without asking after verification" is never turned on, because these rules and scripts are the referee.
- Other repositories pin this one by 40-character SHA, never by branch or tag. Changing that pin is T4 in those repositories too.
- Scripts use only the Python standard library and must run on macOS `/usr/bin/python3` (3.9) and on Ubuntu.

## License

[MIT](LICENSE)
