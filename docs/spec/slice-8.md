# Spec：切片 8，准入只认机器人发的接管评论（第二批）

> 2026-11-22，autopr-design。总账 `research/2026-11-18-自动PR系统设计/batch-2-auto-merge-spec.md`（第二批规格，用户已批准，原话"批准"）的切片 8，对应假设 B3、B4。
> 判据来源：总账 `research/2026-11-21-GitHub机器人身份/README.md` 第三节第 2 小节。

## 做什么

1. 新增 `bot-identity.json`：机器人的 App slug（`haigeerlab-autopilot`）和数字用户 ID（`359574582`）。改它就是改 hub，按 T4 走。
2. 准入取 PR 评论时带上作者字段：`user.login`、`user.id`、`user.type`、`performed_via_github_app.slug`、`created_at`、`updated_at`。
3. 接管评论 `autopilot-takeover:v1` 只有同时满足以下几条才算数：
   - `user.type == Bot`；
   - `user.id` 等于配置里的数字 ID（只核对 ID，不看登录名，所以 App 改名后仍然认）；
   - App slug 等于配置；
   - `updated_at == created_at`，即发出后没被编辑过。字段缺失一律当作编辑过。

   不合格的接管评论一律当不存在，并在摘要里写一行"忽略了 N 条接管评论"。
4. hub 没有 `bot-identity.json`，或文件格式不对时：不认任何接管评论，结论永远是"不会自动合并（没配机器人身份）"。
5. 准入找自己那条摘要评论时，加一个条件：作者必须是 `github-actions[bot]`。别人贴一条同样开头的评论，准入不会去覆盖它。
6. 删掉旧提示"接管评论的作者暂不校验"。

## "编辑过"怎么判断（2026-11-22 实测，hub#26）

用机器人身份发一条评论，再用人的账号编辑它：
- REST 的 `user`、`performed_via_github_app` 都不变；
- `updated_at` 从等于 `created_at` 变成编辑时间；
- GraphQL 的 `lastEditedAt`、`editor` 从空变为有值。

给评论加表情不改 `updated_at`。所以准入只用 REST 的 `updated_at != created_at` 判断编辑，不需要 GraphQL。编排只发新评论、从不编辑，不会误伤自己。

## 不变的

风险档、硬卡（切片 9 才接证据）、降级三项都照旧；检查结果仍是 `neutral`。调用方的调用文件不用改，钉升到含本切片的 hub 提交即可。
