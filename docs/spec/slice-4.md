# Spec：切片 4，风险分级（第一批）

> 2026-11-21，autopr-design。用户选定第一批从风险分级开始（原话"从风险分级开始"）。用户已批准本规格与 A1–A8（原话"批准"）。
> 上游：总账 `research/2026-11-21-自动接管合并方案.md`）第 5 节"风险分级"、8.1 安全内核、8.10 第一批；设计 r6 §3.1（风险档 T0–T4 与上调规则）；类别模板草案 `config/risk-tiers.categories.example.yml`。

## 0. 假设（请先确认）

| # | 假设 | 不成立时 |
| --- | --- | --- |
| A1 | **风险档决定"能不能自动合"**：T0–T2 在档位允许时可以自动合；**T3** 要 Codex 审过且由你合；**T4** 停下交你（方向文档第四节"改到 T4 路径停下"）。取代旧设计里"风险档 → L 级上限"那张表 | 改成 T3 也可自动合，T4 才停 |
| A2 | **规则分两层**：中心仓库放四类模板（插件、契约、前端、内容）和所有类别共用的规则；各仓库只写一行 `extends: <类别>`，需要时补几条本仓库路径。仓库没写时按"通用"处理：只用共用规则，**没匹配到的路径一律算 T3**（宁严勿松） | 没写就按插件类处理 |
| A3 | **第一版的配置文件用 JSON**（`.github/autopr/risk-tiers.json`）：中心仓库的判定脚本只用标准库，没有 YAML 解析器。体检批次搬来 YAML 子集解析器后，再改回 `.yml` | 现在就搬 YAML 解析器（多约 0.5 天） |
| A4 | **上调规则照设计 §3.1.2**：非测试改动超过 200 行或 8 个文件 +1 档；动了依赖清单或锁文件至少 T3；删除或削弱测试断言直接 T4；压制注释净增加直接 T4 | — |
| A5 | **spec-guard 模块产物**（`spec/**`、`tasks/**`、`.agent/**`）：只勾 todo、Spec 指纹没变按普通改动定档；Spec 本身有改动算 T4（合并方案 Q17，r2 已定） | — |
| A6 | **"高风险路径"给 spec-guard 的只读接口**：本机命令输出 T3 和 T4 两张路径清单（JSON），spec-guard 自己决定"碰到哪张算重要需求"。我建议它按"碰到 T3 或 T4 就算" | 只给 T4 |
| A7 | **这一版先不做**：字段级规则（如只看 `package.json` 的 `version` 字段）、跨仓库接口规则（`contracts.yml` 还没建）、内容仓库"删节点"判断。`package.json` 整个文件先算 T3 | — |
| A8 | 仍是**只报告**：准入把风险档和理由写进检查结果，不拦截、不合并 | — |

## 1. 目标

每个 PR 的准入结果里多出一行：**风险档 T0–T4、是哪条规则定的、能不能自动合**。同时给 spec-guard 一个本机命令，读出某个仓库的高风险路径清单。

## 2. 规则

**所有类别共用**（按设计 §3.1 草案）：

| 档 | 路径 |
| --- | --- |
| T4 | `.github/**`、`LICENSE`、`spec/**`、`tasks/**`、`.agent/**`（后三项按 A5） |
| T3 | `CLAUDE.md`、`AGENTS.md`、依赖清单与锁文件（`package.json`、`pnpm-lock.yaml`、`package-lock.json`、`requirements*.txt`、`pyproject.toml`、`uv.lock` 等） |
| T0 | `README*.md`、`CHANGELOG.md` |
| 没匹配到 | T3（A2） |

**四类模板**沿用 `config/risk-tiers.categories.example.yml` 的路径，去掉 A7 里不做的部分：
- 插件：`.claude-plugin/**`、`.codex-plugin/**`、`**/plugin.json` 为 T4；`commands/**`、`skills/**` 为 T2；测试文件 T1；`docs/**` T0。
- 契约：全部 T4。
- 前端：鉴权、会话、令牌、迁移、`wrangler.*`、`.env*` 为 T4；公共 API 入口、Service Worker、中间件、`tsconfig*.json`、`browserslist` 为 T3；`src/**`、`packages/**`、`apps/**` T2；测试 T1；文档 T0。
- 内容：`REQUIREMENTS.md`、`REVIEW_BRIEF.md`、`_meta/**` T4；`maps/**`、`site/**`、`scripts/**` T3；正文目录 T2；`research/**` T1。

**合并规则**：共用规则、类别模板、本仓库补的路径三层**一起匹配，同一文件命中多档取最高**——本仓库只能补充或收紧，不能把模板和共用规则里的档降低（实现时细化：草案里"本仓库路径优先于模板"会允许仓库把 `.github/**` 这类 T4 降下来，和安全内核冲突）；没有任何规则匹配的文件按类别的默认档（未声明类别时 T3）；一个 PR 的风险档 = 所有改动文件的最高档，再套上调规则。

**上调规则的判定方式**（只读 PR 的改动内容，不执行任何代码）：
- 改动行数、文件数：从 PR 文件列表统计，测试文件不计。
- 删除或削弱断言：测试文件里，删除行中含断言关键字（`assert`、`expect(`、`assertEqual`、`should` 等）的数量多于新增行中的数量。
- 压制注释净增加：新增行里的压制标记（`# noqa`、`eslint-disable`、`@ts-ignore`、`@ts-expect-error`、`# type: ignore`、`shellcheck disable`、`pylint: disable`、`//nolint`、`autopilot-allow`）多于删除行里的。

## 3. 输出

准入检查结果里新增一步 `risk`：

```
risk | ✅ | 风险档 T3：依赖清单 package.json 改动（至少 T3）；改动 3 个文件 42 行；可自动合：否（T3 要你合）
```

机器可读部分写进准入的 JSON 结果：`{"tier": "T3", "reasons": [...], "auto_merge_eligible": false, "stop": false}`。T4 时 `stop: true`，并写明是哪条。

**给 spec-guard 的只读命令**（在本机、对仓库的本地检出运行）：

```bash
python3 -B <hub>/risk/cli.py paths --repo <本地检出路径> --json
```

输出：`{"schema": "autopilot.risk-paths/v1", "category": "plugin", "T4": [...], "T3": [...]}`，路径是通配符，语义和切片 1 的 `paths` 一致（`**` 可跨目录，`*` 只匹配一层）。

## 4. 放在哪里

| 东西 | 位置 |
| --- | --- |
| 模板与共用规则 `risk/templates.json`、判定脚本 `risk/`、单元测试 | autopilot-hub（T4） |
| 准入接入风险档（新增 `risk` 一步） | autopilot-hub 的 `admission/` |
| 各仓库的 `.github/autopr/risk-tiers.json` | 可选；试点 agent-relay 写一行 `{"extends": "plugin"}`，经 ecosystem-lead 请负责人提交 |

## 5. 验收标准

1. 单元测试覆盖：
   - 四类模板各一个"文件 → 档"的例子；
   - 没匹配到算 T3；
   - 本仓库路径优先于模板；
   - 多个文件取最高；
   - 四条上调规则各自"触发"和"不触发"；
   - A5 的两种情况（只勾 todo、改了 Spec）；
   - T4 时 `stop: true`。
2. 用 hub 自己过去的几个 PR（#3–#10）和 agent-relay 最近的 PR 做回放，打印各自的风险档，你抽看几条，确认和你的直觉一致。
3. spec-guard 用 `paths` 命令读 agent-relay 的本地检出，拿到 T3、T4 两张清单。

## 6. 依赖与分工

| 谁 | 做什么 |
| --- | --- |
| autopr-design | 规格、脚本、测试、hub 的 PR、回放 |
| 你 | 批准本规格（含 A1–A8）；合并 hub 的 PR；抽看回放结果 |
| sg-coord | 接 `paths` 命令，做"碰高风险路径算重要需求" |
| ecosystem-lead → agent-relay-owner | 提交 `.github/autopr/risk-tiers.json`（一行） |
