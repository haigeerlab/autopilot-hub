# CLAUDE.md

Claude Code 进入本仓库时读这份文件。

<!-- shared:begin -->
## 项目规矩（Claude Code 与 Codex 共用，两份文件这一段逐字相同）

本仓库是 haigeerlab 自动维护系统 autopilot 的中心仓库：各插件仓库共用的准入、合并硬卡、降级、风险分级、规则检查、体检、发布流程和合规监测都在这里，各仓库按 40 位提交 SHA 引用。开发规范见 [docs/plugin-standard.md](docs/plugin-standard.md)。

- **全部改动都属于 T4**：只开 PR；开完 PR 后会话自己在后台等 CI（一条后台命令 `gh pr checks <号> --watch`，跑完宿主会叫醒会话，不反复轮询），通过就主动问用户可以合并吗，不通过就自己修、修不了再告诉用户；用户在对话里说「合并」后，会话先看 PR 状态：必需检查已全部通过（mergeStateStatus 为 CLEAN）时直接执行 `gh pr merge --squash`，否则执行 `gh pr merge --squash --auto` 等 `CI required` 通过后由 GitHub 合并；会话核实已合并、记下合并提交再告诉用户（规范 F3，v1.6）。永不开「合并：自动」（验证通过不问就合）。
- **只用 Python 标准库**，兼容 macOS 自带的 `/usr/bin/python3`（3.9）。改完跑两遍测试：
  `python3 -B -m unittest discover -s tests` 和 `/usr/bin/python3 -B -m unittest discover -s tests`。
- **先写测试再实现**；每个切片的规格写在 `docs/spec/`，README 的「现状」同步一行。
- **提交说明、PR 标题和正文用中文**，第一行带类型前缀（feat / fix / docs / chore / test / ci / refactor）。代码和注释沿用仓库原有的英文。
- **公开仓库，不写本机路径和用户名**（`tests/test_no_local_paths.py` 会拦）；令牌、私钥、JWT 不进文件、不进日志，只经标准输入或环境变量传递。
- **改到调用方会执行的部分要通知升钉**（规范 H1）：`.github/workflows/`、`admission/`、`degrade/`、`rules/`、共享检查代码、体检配置格式。合并后由 autopr-design 发"需要升钉"通知，各仓库统一升到同一个基线。
- **不读、不上传用户的 kb 知识库仓库**；只有 `kb-plugin` 在接入范围内。
<!-- shared:end -->

## 只给 Claude Code

- 本仓库的实现由 autopr-design 会话负责；改插件的需求先发 ecosystem-lead，咨询和缺陷可以直接找对应插件的会话。
- 发给其他会话的消息，先把草稿给用户看，用户同意后再发。
