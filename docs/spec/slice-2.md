# Spec：切片 2，自动降级（GitHub 侧，只开降级 issue）

> 2026-11-20，autopr-design。用户选定切片 2 先做自动降级（原话"自动降级"），并批准本规格与 A1–A6 六条假设（原话"批准"）。
> 上游：设计 r6 总账 `research/2026-11-18-自动PR系统设计/README.md` §3.4 降级、§6.1 成功标准 ③「出事停得住」；切片 1 规格（autopilot-hub `docs/spec/slice-1.md`）。

## 0. 假设（请先确认）

| # | 假设 | 不成立时 |
| --- | --- | --- |
| A1 | **"autopilot 的 PR"靠标签 `autopilot:authored` 认定**。agent 身份（设计 Q2）建立之前没有更可靠的办法；以后由编排打这个标签。现在一个这样的 PR 都没有，所以本切片上线后，只能用演练 PR 来触发 | 改为"所有经过准入的 PR 都算"，但这样人自己 revert 自己的 PR 也会降级 |
| A2 | **降级只开 issue，不调本机命令**。`module-crew tier degrade` 由以后的编排在本机拾取 issue 后调用，不在本切片 | — |
| A3 | **降到的等级用绝对值**：revert、主干变红都降到 **L2**（设计 §3.4"最高降到 L2"）；你手动打 `autopilot:regression` 标签也降到 L2；`autopilot:freeze` 标签冻结到 **L0**。眼下各仓库最高只到 L2，所以降到 L2 暂时看不出效果，演练时用 freeze 看 L0 | 改成"在当前有效等级上降一级"，需要先能算出当前等级，复杂得多 |
| A4 | **主干变红立刻降级**，不等设计稿里写的 2 小时。理由是更早停下更安全，而且不用定时任务计时。代价是一次偶发失败也会降级，要你关 issue 解除 | 保留 2 小时：要加定时触发和计时 issue |
| A5 | **作用范围是整个仓库**（`tier: "*"`）。本切片没有风险档，按档位细分留到风险档切片 | — |
| A6 | 被降级的仓库各自放一个调用方文件 `.github/workflows/autopilot-degrade.yml`（T4，经 ecosystem-lead 请仓库负责人提交、你合并）。先只在 agent-relay | 合并进现有的 `autopilot-admission.yml`：少一个文件，但两种触发混在一起不好读 |

## 1. 目标

出事时，GitHub 上**不依赖你的 Mac 在线**，自动开出一个 `autopilot:degrade` issue；切片 1 的准入已经会读它，下一个 PR 的上限就被压低。解除降级 = 你关掉这个 issue。

对应成功标准 ③：降级在下一个 PR 准入前生效；漏停为 0。

## 2. 范围

| 做 | 不做 |
| --- | --- |
| 触发 1：main 上出现 revert 提交，被 revert 的提交属于带 `autopilot:authored` 标签的 PR → 降到 L2 | 判断"7 天内两次 revert"之类的累计规则 |
| 触发 2：main 上的主 CI 失败，且失败的那个提交是 `autopilot:authored` PR 的合并 → 降到 L2 | 2 小时计时；主干恢复绿色后自动解除 |
| 触发 3：任意 issue 被打上 `autopilot:regression` → 降到 L2；打上 `autopilot:freeze` → 冻结到 L0。这是你的"手动刹车"，也给以后的巡检用 | 巡检本身（`post-release-patrol`） |
| 幂等：同一原因、同一引用（提交或 issue）已有未关闭的降级 issue 时，只追加评论，不重复开 | 闸门被放松的检测（要风险档） |
| 降级 issue 的正文给人看得懂：为什么降、降到几级、怎么解除 | 开纠正事件 issue（留给纠错晋升切片） |
| 以只读方式拉取信息，只写 issue | 调 `module-crew tier degrade`（A2） |

## 3. 降级 issue 的格式

- 标题：`autopilot 降级：<原因>（<引用>）`，例如 `autopilot 降级：revert（abc1234）`。
- 标签：`autopilot:degrade`。
- 正文含机器可读块，格式沿用切片 1 已能解析的写法：

```html
<!-- autopilot-degrade:v1 {"scope":{"repo":"haigeerlab/agent-relay","tier":"*"},"level":"L2","reason":"revert","ref":"<提交 SHA 或 issue 链接>","evidence":["<链接>"]} -->
```

- `reason` 取值：`revert`、`main-red`、`regression`、`freeze`。
- 幂等键：`reason` 加 `ref`。

## 4. 判定细节

**revert**（`push` 到默认分支）：
- 逐个看这次推送的新提交；提交说明以 `Revert "` 开头，并且正文里有 `This reverts commit <40 位 SHA>`。
- 用 API 查这个被 revert 的提交属于哪个 PR（`commits/{sha}/pulls`），PR 带 `autopilot:authored` 才降级。
- 查不到 PR、或 PR 没有这个标签：不降级，在运行摘要里写一行原因。

**main-red**（`workflow_run`，主 CI 在默认分支上完成且结论为 `failure`）：
- 取那次运行的 `head_sha`，查它属于哪个 PR；带 `autopilot:authored` 才降级。
- 主 CI 的 workflow 名由调用方传入（agent-relay 是 `CI`）。

**regression、freeze**（`issues` 的 `labeled` 事件）：
- 标签名对上就降级，`ref` 是这个 issue 的链接。
- 打标签本身需要仓库写权限，所以不额外校验是谁打的。

**fail closed 的边界**：任何一步 API 调用失败，都**不**静默放过：运行标红（workflow 失败），让你在 Actions 里看得见。不会因为出错而自动开出降级 issue，因为那样会被一次网络故障误伤。

## 5. 安全

- 不 checkout、不执行任何 PR 代码；判定脚本从中心仓库按 40 位 SHA 取，和切片 1 相同。
- 提交说明、issue 标题等外部文本只经环境变量进入脚本，不拼进 `run:`。
- 权限：`contents: read`、`issues: write`、`pull-requests: read`、`actions: read`。
- 降级只会压低上限。伪造一个降级 issue 最多让自己的仓库变严，不会放宽；放宽（关 issue）需要仓库写权限的人亲手操作。

## 6. 放在哪里

| 东西 | 位置 | 改动档 |
| --- | --- | --- |
| 判定脚本 `degrade/` + 单元测试 | autopilot-hub | 中心仓库全部 T4 |
| 可复用 workflow `.github/workflows/degrade.yml` | autopilot-hub | T4 |
| 调用方示例 `examples/caller-autopilot-degrade.yml` | autopilot-hub | T4 |
| 调用方文件 `.github/workflows/autopilot-degrade.yml` | agent-relay | T4（经 ecosystem-lead，用户合并） |

## 7. 验收标准

1. 单元测试覆盖：
   - 三类触发各自的"降"与"不降"；
   - 幂等，不重复开；
   - 提交说明格式不对、查不到 PR、PR 没有标签；
   - API 失败时运行标红而不是开 issue。
2. 中心仓库自己的演练：在 hub 上，给一个测试 issue 打 `autopilot:freeze`，自动开出降级 issue；再开一个 PR，准入详情的 limits 一步列出"未关闭的降级 issue 最低 L0"；关掉降级 issue 后，下一次准入的这一行消失。（hub 和 agent-relay 现在的上限本来就是 L0，所以看的是 limits 那一行，而不是标题里的等级。）
3. agent-relay 接入调用方文件后，同样演练一次 freeze，然后关闭解除。
4. revert 和 main-red 两条用单元测试覆盖。真实演练要等编排能产出带 `autopilot:authored` 的 PR，记为后续验收。

## 8. 依赖与分工

| 谁 | 做什么 |
| --- | --- |
| autopr-design | 写规格、判定脚本、workflow、测试；开 hub 的 PR |
| 用户 | 批准本规格；合并 hub 与 agent-relay 的 PR；演练后关掉降级 issue |
| ecosystem-lead → agent-relay-owner | 提交 agent-relay 的调用方文件 |
