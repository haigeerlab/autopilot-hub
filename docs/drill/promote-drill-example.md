# 演练：晋升 drill-example（不合并）

> 切片 6 验收第 4 条的演练产物。只验证流程：纠正事件 → 晋升候选 → 按 `skills/correction-promotion/SKILL.md` 起草晋升 PR → 关闭。不写真检查器，PR 关闭不合并。

## 1. 确认这是一个类

- 错误写法：测试里写死当前日期或时间（例如 `today = "2026-11-21"`），到点后测试失效。
- 正确写法：把"现在"作为参数注入（例如 `--today`、`now=` 参数），测试里传固定值。
- 事件：#16、#17、#20、#22（#18、#21 是修复读回前的重复记录）；候选 #19。

## 2. 逐层判断（Q1–Q5）

- Q1 架构：能。把取当前时间收进一个函数（如 `rules/common.py` 的 `parse_today`），业务代码只经它取时间——hub 的 `rules/` 已经这样做，所以本类在 hub 里已没有入口。
- Q2 类型：不需要。
- Q3 检查器：可选，止血用：测试文件里新增的 `date.today()`、`datetime.now()` 调用报错（只拦新增）。
- Q4、Q5：不需要。

## 3. 若真做，检查器的报错（六行模板）

```text
fixed-clock: 测试里直接取了当前时间（datetime.now()）
为什么：到点后测试结果会变，时好时坏
改成：把"现在"作为参数传入，测试里传固定值，例如 decide(now=FIXED_NOW)
不要：不要用 sleep 或把日期写得很远来绕过
例外：只能写在本行，格式 # autopilot-allow fixed-clock: <理由> until <YYYY-MM-DD> approved-by <人>
详见：docs/spec/slice-6.md
```

## 4. 失败证明（演练不做）

真晋升时要在一个真实历史出错提交上跑出失败、在 main 上跑出通过；本演练不做。
