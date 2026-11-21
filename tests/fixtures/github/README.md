# GitHub 录制夹具

搬自 repo-readiness（`ac13f17`）的 `tests/fixtures/github/` 里的 `agent-relay/`、`pwa-platform/` 两个目录，2026-11-19 / 20 用只读 `gh api --method GET` 录制，不联网即可回放。每个文件是一次请求的状态、HTTP 码、正文，按请求路径 sha256 前 16 位命名（见 `checkup/ghprobe.py` 的 `ReplayTransport`）。

| 目录 | 用来测什么 |
| --- | --- |
| `agent-relay/` | C3 旧版保护只禁强推、禁删除、没要求 PR；C4、C6、C7 |
| `pwa-platform/` | C3 靠规则集满足；C4、C6、C7 |

`tests/fixtures/workflows/` 是同一来源的 5 个公开仓库 CI workflow 原样复制，`tests/fixtures/yaml/interface.example.yml` 是接入标准的 interface.yml 样例。
