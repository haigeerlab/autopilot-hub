# CLAUDE.md

Claude Code 进入本仓库时读这份文件。

<!-- shared:begin -->
## 项目规矩（Claude Code 与 Codex 共用，两份文件这一段逐字相同）

本仓库是 haigeerlab 自动维护系统 autopilot 的中心仓库：各插件仓库共用的准入、合并硬卡、降级、风险分级、规则检查、体检、发布流程和合规监测都在这里，各仓库按 40 位提交 SHA 引用。开发规范见 [docs/plugin-standard.md](docs/plugin-standard.md)。

- **全部改动都属于 T4**：只开 PR；开完 PR 就问用户「PR #N 已开，CI 正在跑，可以合并吗」，不等 CI 跑完再问，也不在后台用 `gh pr checks --watch`、定时或循环自己等 CI（桌面版不允许会话自己轮询 CI）。用户在对话里说「合并」后：已 CLEAN 就直接 `gh pr merge --squash`；检查没跑完就 `gh pr merge --squash --auto`，GitHub 等 `CI required` 通过后合并，不过就不合；同时为这个 PR 打开 Auto-fix（CI 失败、冲突、评审意见时叫醒会话去修）。下次被叫醒或用户下次来时，核实已合并、记下合并提交再告诉用户（规范 F3，v1.7）。永不开「合并：自动」（验证通过不问就合）。
- **只用 Python 标准库**，兼容 macOS 自带的 `/usr/bin/python3`（3.9）。日常改完跑 `scripts/validate.sh --quick`；合并前跑全量，两遍：
  `scripts/validate.sh` 和 `PYTHON=/usr/bin/python3 scripts/validate.sh`（规范 C2）。
- **先写测试再实现**；每个切片的规格写在 `docs/spec/`，README 的「现状」同步一行。
- **提交说明、PR 标题和正文用中文**，第一行带类型前缀（feat / fix / docs / chore / test / ci / refactor）。代码和注释沿用仓库原有的英文。
- **公开仓库，不写本机路径和用户名**（`tests/test_no_local_paths.py` 会拦）；令牌、私钥、JWT 不进文件、不进日志，只经标准输入或环境变量传递。
- **改到调用方会执行的部分要通知升钉**（规范 H1）：`.github/workflows/`、`admission/`、`degrade/`、`rules/`、共享检查代码、体检配置格式。合并后由 autopr-design 发"需要升钉"通知，各仓库统一升到同一个基线。
- **不读、不上传用户的 kb 知识库仓库**；只有 `kb-plugin` 在接入范围内。
<!-- shared:end -->

## 只给 Claude Code

- 本仓库的实现由 autopr-design 会话负责；改插件的需求先发 ecosystem-lead，咨询和缺陷可以直接找对应插件的会话。
- 发给其他会话的消息，先把草稿给用户看，用户同意后再发。
