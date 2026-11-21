# Spec：切片 10，本机编排与合并（第二批）

> 2026-11-22，autopr-design。总账 `research/2026-11-18-自动PR系统设计/batch-2-auto-merge-spec.md`（第二批规格，用户已批准，原话"批准"）的切片 10，对应假设 B1、B2、B6、B8、B9。

## 用法

```bash
python3 -B orchestrator/cli.py run --repo haigeerlab/autopilot-sandbox --pr 2            # 真跑：可能合并
python3 -B orchestrator/cli.py run --repo haigeerlab/autopilot-sandbox --pr 2 --dry-run  # 只读令牌：跑检查和审查，不发评论、不合并
```

退出码：0 = 已合并或预演通过；1 = 停下（原因写在 PR 上并打印出来）；2 = 环境问题（换不出令牌、找不到 delegate）。

## 一次运行

1. **令牌**：从钥匙串读私钥，经标准输入交给 `openssl` 签 JWT；JWT 经标准输入交给 `curl`，换安装令牌。所以私钥和 JWT 都不出现在文件或命令行参数里。令牌只给当前仓库，权限只给 contents、pull_requests、issues 的写（`--dry-run` 只给读）。运行结束就作废。
2. **PR 检查**：PR 要是打开的、不是草稿、不来自 fork；作者只能是机器人或仓库所有者，否则不在本机跑它的代码。
3. **降级**：有未关闭的降级 issue → 停。
4. **接管档位**：读基础分支的 `.github/autopr/takeover.yml`（`merge: auto|human`），以机器人身份发 `autopilot-takeover:v1` 评论。合并不是 `auto` → 停。spec-guard 项目改读 `/map --json`，等 spec-guard 0.91 发布后再接（见"与规格的差异"）。
5. **风险预检**：用和准入同一份代码算风险档。不是 T0–T2 就停，省一次审查。
6. **检查**：建一次性克隆，取基础分支最新提交和 PR head。检查命令从**基础分支**的 `interface.yml` 或 PR workflow 里找，PR 改不了它。命令在 PR head 上运行，环境变量只留 PATH、HOME 这类基本项，不带令牌；超过 15 分钟算失败。
7. **审查**：`delegate-exec.sh review --base <merge-base> --head <head>`，用 0.10.0 以上的版本。
   - 没有结论（Codex 不可用、执行失败、`inconclusive`）→ 停，并在 `~/.autopilot/ledger.jsonl` 记一条 `codex_unavailable`。不会退回别的审查方式。
   - 有没审到的文件，或报告里没有补丁指纹 → 停。
   - 有 blocking（`severity` 缺失也按 blocking 算）：第 1 轮 → 停，交作者返工；作者推了新提交再运行时，带 `--previous-report` 复审，记为第 2 轮；第 2 轮仍有 blocking → 停，交你。
   - 同一个 head 已有有结论的审查，就直接沿用，不重复花 Codex 额度。报告存在 `~/.autopilot/reviews/<owner>__<repo>/<PR>.json`。
8. **证据**：以机器人身份发 `autopilot-evidence:v1` 评论（格式见切片 9），附检查输出的最后 40 行。
9. **准入**：用和 `admission.yml` 同一份代码、重新取的输入在本机重算一遍。不是"可以自动合并"就停。
10. **合并**：`PUT pulls/<n>/merge`，带 `sha=<审过的 head>`（等同 `--match-head-commit`）。合并方式优先 squash，其次 merge。
11. **读回**：`merged == true` 且有 `merge_commit_sha` 才算成功；然后打上 `autopilot:authored` 和 `autopilot:auto-merged` 两个标签。

每次停下，都以机器人身份在 PR 上留一条 `<!-- autopilot-run:v1 -->` 评论，写明停在哪一步、为什么。

## 与规格的差异（请留意）

- **准入在本机重算**（B9 原写"合并前再拉一次准入结果"）：发评论不会让 Actions 里的准入重跑。准入是 `pull_request` 触发的，而调用方仓库加 `issue_comment` 触发拿不到 PR head 的上下文。所以合并前，编排用同一份准入代码、重新取的评论、文件、风险配置和降级 issue 在本机重算。Actions 里那条准入摘要停在"发证据之前"的状态，直到 PR 再有推送。
- **接管档位暂时只读 `takeover.yml`**：spec-guard 0.91（`/map --json` 带接管档位）还没发布，沙盒暂时不是 spec-guard 项目，所以最终验收这一项跳过（B7 的非 spec-guard 分支）。0.91 发布后补上读取，沙盒也装上 spec-guard。
- **只接机器人或仓库所有者开的同仓库 PR**：规格没写这一条。加上是因为编排会在你的 Mac 上运行 PR 的检查命令。

## 安全

- 私钥、JWT、令牌都不写日志、不写文件、不进评论。git 认证用 `GIT_CONFIG_*` 环境变量传给 git，不进命令行参数。
- 检查命令和 delegate 运行时，环境里没有 `GH_TOKEN`、`GITHUB_TOKEN`。
- 准入四项和机器人身份都由 autopilot 自己的代码判定，不依赖仓库里的流程配置。
