---
name: retro
description: 工程周复盘——从 git 历史自动计算指标（提交数/LOC/测试比/fix 比/会话模式/高峰时段/连续发布记录），按人拆分并给出具体表扬与成长建议，周环比趋势，存 JSON 快照供下次对比。用于"retro / 周复盘 / 这周怎么样 / 复盘一下"。
version: 1.0.0
category: dev-workflow
author: QunWork (蒸馏自 gstack/retro, MIT)
tags: retro, weekly, metrics, git-analysis
---

# 工程周复盘（retro）

## 何时使用

用户要周复盘、"这周怎么样"、或定时任务周期触发。默认窗口 7 天（可指定）。数据源：git 历史 + 项目记录（TODOS、测试文件、发布记录）。

## 数据收集（基于 git）

- 提交与作者：`git log --since=<窗口起点> --pretty=format:'%h|%an|%ad|%s' --date=format:'%Y-%m-%d %H:%M'`
- 变更量：`git log --since=... --numstat` 统计 insertions/deletions（net LOC）
- 测试 LOC：同窗口内 `*test*`/`*spec*` 文件的变更量
- 活跃日：按本地日期去重提交日期
- 作者身份：`git config user.name` 识别"你"；解析 `Co-Authored-By:` 尾注归功协作者（AI 协作者单独计为"AI 辅助提交"指标，不当团队成员）

## 指标计算

1. **概要表**：提交数、贡献者数、insertions、deletions、net LOC、测试 LOC、测试比（test LOC / total LOC）、活跃天数、PR/合并数（有 gh 时）。
2. **时间模式**：按小时聚合提交 → 高峰时段、死区、双峰还是连续、深夜（22 点后）编码簇。
3. **工作会话检测**：提交间隔 **45 分钟**为会话切分点。分类：深会话（50+ 分）、中会话（20-50 分）、微会话（<20 分，多为单提交即走）。计算：总活跃编码时间、平均会话时长、活跃时间每小时 LOC。
4. **提交类型分布**：按 conventional commit 前缀（feat/fix/refactor/test/chore/docs）分组，百分比条形图。**fix 占比 >50% 要标记**——"快发快修"模式可能说明审查有缺口。
5. **热点分析**：top 10 变更文件；标出改 5+ 次的（churn 热点）；热点列表里测试文件 vs 生产文件；VERSION/CHANGELOG 更新频率（版本纪律指标）。
6. **PR 规模分布**：按 commit diff 估算：Small <100 LOC / Medium 100-500 / Large 500-1500 / XL 1500+。
7. **聚焦分数**：单最常改顶层目录的提交占比。高=深度聚焦；低=频繁上下文切换。输出："Focus score: 62% (app/services/)"
8. **本周之星**：窗口内最大单个 PR/变更（按 LOC），给出编号、标题、LOC、为什么重要（从 commit message 与文件推断）。
9. **连续发布记录（streak）**：从今天往回数，连续每天至少有 1 个提交到默认分支的天数（团队 + 个人）。

## 团队成员分析

每个贡献者：提交数与 LOC、关注领域（top 3 目录）、提交类型混合、编码时段、会话数、测试纪律（个人测试 LOC 比）、最大贡献（单最高影响提交/PR）。

- **当前用户（"你"）**：最深度的处理——会话分析、时间模式、聚焦分数全给，用第一人称。
- **每位队友**：2-3 句做了什么 + 模式，然后：
  - **表扬（1-2 件具体的事）**：锚定真实提交。不是"干得好"——说出具体好在哪。"3 个专注会话里重写了整个 auth 中间件，测试覆盖 45%""每个 PR 都 <200 LOC——拆分很自律。"
  - **成长建议（1 件具体的事）**：以升级建议而非批评的框架，锚定真实数据。"本周测试比 12%——趁支付模块还没变复杂补上测试覆盖会回报很高""同一文件 5 个 fix 提交说明原 PR 当时该过一遍审查。"

## 周环比趋势（窗口 ≥14 天时）

按周分桶显示：每周提交数（总量与每人）、每周 LOC、每周测试比、每周 fix 比、每周会话数。

## 与上次复盘对比

读上次复盘 JSON 快照，算关键指标 delta：
```
                    Last        Now         Delta
Test ratio:         22%   →    41%         ↑19pp
Sessions:           10    →    14          ↑4
Fix ratio:          54%   →    30%         ↓24pp (improving)
```
无历史 → 附加"首次复盘——下周再跑一次看趋势。"

## 保存快照

写 JSON 快照（如 `.context/retros/<日期>-<序号>.json`）：date、window、metrics（commits/contributors/insertions/deletions/net_loc/test_loc/test_ratio/active_days/sessions/deep_sessions/avg_session_minutes/loc_per_session_hour/feat_pct/fix_pct/peak_hour/ai_assisted_commits）、authors（每人 commits/insertions/deletions/test_ratio/top_area）、streak_days、tweetable 摘要。测试健康（有测试文件时）：total_test_files/tests_added/regression_test_commits。积压（有 TODOS.md 时）：total_open/p0_p1/completed/added。无数据的字段省略。

## 输出叙述

**tweetable 摘要**（第一行）：
```
Week of Mar 1: 47 commits (3 contributors), 3.2k LOC, 38% tests, 12 PRs, peak: 10pm | Streak: 47d
```

然后：**概要表 → 趋势对比 → 时间与会话模式叙述**（最活跃时段及驱动因素；会话在变长还是变短；估算日均活跃编码时长；队友同频还是轮班）**→ 发布速度叙述**（提交类型混合揭示了什么；PR 规模分布揭示发布节奏；同子系统 fix 链检测；版本纪律）**→ 代码质量信号**（测试比趋势；热点是否在反复改同一文件）**→ 成员分析**（含表扬与成长建议）。

## 重要规则

- 一切锚定真实数据：表扬与建议必须引用具体提交/数字，禁止空洞夸奖。
- fix 比 >50%、同一文件反复改、测试比下降——这些信号要显式指出。
- 首次复盘注明"首次记录"；快照供下周对比。
- 数据来自 git 历史与项目记录，不编造。
- 沉淀：复盘发现的非显然模式按 post-task-self-review 惯例 remember 保存。
