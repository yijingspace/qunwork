---
name: ship
description: 发布工程师模式——把分支发布出去：同步基线、跑测试、审计覆盖率、合流前审查、整理可二分提交、推送、开 PR、同步文档。铁律：没有新鲜的验证证据不许宣称完成。用于"ship / 发布 / 推这个分支 / 开 PR / 发版本"。
version: 1.0.0
category: dev-workflow
author: QunWork (蒸馏自 gstack/ship, MIT)
tags: ship, release, pr, publish, git
---

# 发布流程（ship）

## 何时使用

用户说"发布/推分支/开 PR/发版本"时。目标是：用户说 ship，接下来看到的是审查结论 + PR URL + 已同步的文档。

## Step 1：预检

1. **分支检查**：在基线上或默认分支 → 中止："你在基线上。从功能分支发布。"
2. `git status`：未提交改动总是纳入，不必问。
3. `git diff <base>...HEAD --stat` + `git log <base>..HEAD --oneline` 了解发布内容。
4. **评审就绪仪表盘**：检查本分支最近（7 天内）的评审记录——
   - **Eng Review（工程评审）是唯一门禁**：review（diff 级）或 plan-eng-review（计划级）任一新近且 status=clean → CLEARED；缺失/过期/有问题 → NOT CLEARED，打印"未找到先前工程评审——ship 会在 Step 9 自己跑合流前审查"。**NOT CLEARED 不阻塞**，ship 自带审查兜底。
   - CEO/Design 评审为参考，永不阻塞（产品改动建议 CEO Review；前端改动提示 Design Review）。
   - 大 diff（>200 行）提示可先跑 plan-eng-review。
   - 评审记录过期检测：有 commit 字段的对比 HEAD，落后 N 个 commit 提示可能过期。

## Step 2：分发管道检查

diff 引入新独立产物（CLI 二进制/库/工具）且非已有部署的 Web 服务时，检查有无发布流水线（.github/workflows 的 release/publish，或 .gitlab-ci.yml）。**没有** → ask_user：A) 现在就加发布流水线 B) 延后到 TODOS.md C) 不需要（内部/纯 Web，已有部署覆盖）。有 → 静默继续。无新产物 → 静默跳过。

## Step 3：合并基线（测试之前）

fetch + merge 基线进功能分支，让测试跑在合并后的状态上。

## Step 4-6：测试

跑全量测试套件，粘贴输出。改了提示词/LLM 相关文件时跑对应评估。测试失败 → 停下修，回到测试。**绝不跳过测试。**

## Step 7：覆盖率审计（diff 级）

对 diff 做覆盖率审计：每条新代码路径是否有测试？缺口 → **生成覆盖测试**（符合项目测试惯例），必须通过才能提交。绝不提交失败的测试。

## Step 8：计划完成度审计

有计划文件 → 提取可执行项，逐项对照 diff 判 DONE/PARTIAL/NOT DONE/CHANGED/UNVERIFIABLE（同 review 技能的审计法）。范围漂移检测：改的正是该做的吗？**TODOS.md 完成判定要保守**——只有 diff 清楚显示工作完成才标完成。

## Step 9：合流前审查（内置兜底）

跑 review 方法论（SQL 安全/竞态/LLM 信任边界/注入/枚举完整性两遍清单 + 置信度门）。Fix-First：AUTO-FIX 直接修，ASK 项批量 ask_user。**发现未解决不发布。**

## Step 10-11：评论处理与对抗审查

有 PR 评论 → 分类处理（有效则修、误报则举证回复）。对抗审查（可选增强）：用独立视角审一遍 diff 找结构性问题。均不阻塞，但发现修复后要回到 Step 4 重测。

## Step 12-13：版本号与变更日志

1. **版本号 bump**：MINOR/MAJOR 级 bump 需 ask_user 确认；PATCH 自动。用 4 段版本格式（如 VERSION 文件的 X.Y.Z.W）。
2. **CHANGELOG 条目**：日期 `YYYY-MM-DD`，描述本次实际变更，与版本号一致。

## Step 14：WIP 提交整理（非破坏性）

有 `WIP:` 提交：
- 先把 WIP 提交的上下文导出存为文件（供 CHANGELOG/PR 使用）。
- **绝不盲目 `git reset --soft`**（会撤销真实工作）。只对"纯 WIP 分支"安全（验证分支上无非 WIP 提交后）。
- 混合提交用 rebase 把 WIP 标为 fixup 折叠进前一个提交；冲突 → abort 并请用户手工处理（STATUS: BLOCKED）。

## Step 15：可二分提交

把 diff 按**逻辑单元**组织成小提交（利于 git bisect 和 AI 理解）：
- 顺序：基础设施（迁移/配置/路由）→ 模型与服务（含测试）→ 控制器/视图/组件（含测试）→ **VERSION+CHANGELOG 永远放最后**。
- 模型与其测试同提交；服务与其测试同提交；控制器/视图/测试同提交；迁移单独或随模型。
- 每个提交**独立有效**（无坏引用），依赖先提交。
- 小 diff（<50 行 <4 文件）单提交即可。
- 提交信息：`<type>: <摘要>`（feat/fix/chore/refactor/docs），正文简述。最后提交（版本+变更日志）加版本号与 Co-Authored-By。

## Step 16：验证门（铁律）

**没有新鲜验证证据，不许宣称完成。**
- Step 4-6 之后代码有变（审查修复等）→ **重跑测试**，粘贴新鲜输出。陈旧输出不合格。
- 有构建步骤 → 跑构建。
- 反合理化："应该能行"→ 跑它；"我有信心"→ 信心不是证据；"之前测过"→ 代码已变，重测；"改动很小"→ 小改动也会搞坏生产。
- 此时测试失败 → **停止，不推送**。修完回到测试步骤。

## Step 17：推送

1. **凭据预推守卫**：推送前扫 diff 里的凭据（API key/token/私钥）。首次推送时 offer 装一个 git pre-push 钩子阻止含凭据的推送（守卫非强制，可跳过）。
2. **幂等检查**：`git fetch origin <分支>` 对比 LOCAL/REMOTE commit——已推送则跳过。
3. `git push -u origin <分支>`。**绝不 force push。**
4. 推送完**没完**——文档同步和 PR 是强制收尾。

## Step 18-19：文档同步 + PR

1. **文档同步**：README/架构文档中本次改动涉及的功能描述是否过期？过期则同步（用 document-release 思路：参考/教程/解释/教程覆盖）。CHANGELOG 必含本次条目。
2. **PR**：标题 `v<新版本> <type>: <摘要>`（版本前缀不变量）。正文：改了什么/为什么/测试证据/覆盖/计划完成度/回归测试。若平台 CLI 不可用（无 gh/glab）→ 输出完整 PR 草稿文本供用户粘贴。

## Step 20：持久化指标

把发布指标（覆盖率%、计划完成度、验证结果、版本、分支）记入项目发布记录（如 `docs/releases/<分支>-<日期>.jsonl`），供 retro 技能追踪趋势。**自动执行，不问不跳。**

## 重要规则

- **绝不跳过测试**；失败即停。
- **绝不跳过合流前审查**。
- **绝不 force push**。
- 不要求琐碎确认（"准备好了吗？""开 PR 吗？"）。**该停的**：版本 bump（MINOR/MAJOR）、审查 ASK 项、大 diff 的 P1 级发现。
- 提交按逻辑拆分，可二分。
- 无新鲜验证证据不推送。
- 目标：用户说 ship → 看到审查结论 + PR URL + 已同步文档。
- 沉淀：发布中的非显然坑按 post-task-self-review 惯例 remember 保存。
