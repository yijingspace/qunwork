---
name: qa
description: 网页/应用系统化 QA 测试并修复发现的 bug（test→fix→verify）。按严重级分级修复、每个修复原子提交并重测，产出前后健康评分与可发布性结论。三档：Quick（只修关键/高）、Standard（+中，默认）、Exhaustive（+低/外观）。用于"qa / test the site / 测测这个网站 / 找找 bug / 这功能能用吗"。
version: 1.0.0
category: dev-workflow
author: QunWork (蒸馏自 gstack/qa, MIT)
tags: qa, testing, browser, bug-fix, health-score
---

# 网页 QA：测试 → 修复 → 验证

## 何时使用

用户要求测网站/应用、找 bug、"这功能能用吗"、功能待测时主动建议。纯报告不修复请用 qa-only 技能。

## 参数与模式

| 参数 | 默认 | 说明 |
|---|---|---|
| 目标 URL | 自动探测 | 给了 URL 用 URL；没给且在功能分支上 → diff-aware 模式 |
| 档位 | Standard | `--quick` 只修 critical+high；`--exhaustive` 全修 |
| 模式 | full | diff-aware / full / regression(对比基线) |

**浏览器工具映射（QunWork）**：`browser_open_url` 打开页面、`browser_snapshot` 读页面结构与可交互元素、`browser_screenshot` 截图留证、`browser_click`/`browser_type`/`browser_select` 操作、`browser_wait` 等待。控制台报错可通过浏览器会话或运行脚本探测。

**前提：干净工作区。** 若 `git status` 非空，先 ask_user：A) 提交现有改动 B) stash C) 中止。QA 需要每个修复独立原子提交。

## 模式

### Diff-aware（无 URL 且在有改动的分支——最常见）
1. `git diff main...HEAD --name-only` + `git log main..HEAD --oneline` 分析改了什么。
2. 从改动文件推受影响页面/路由：控制器/路由→URL 路径；视图/组件→渲染页面；模型/服务→引用它的页面；API 端点→直接请求测试。
3. 探测本地应用：依次试 `browser_open_url` localhost:3000/4000/8080；都不行问用户要 URL。
4. 逐个测受影响页面：打开→截图→查控制台报错→交互改动走完整流程→验证预期效果。
5. 对照 commit message/PR 描述判断**意图**：改动应该做什么？实际做了吗？
6. 查 TODOS.md 相关已知问题；新发现的 bug 记入报告。
7. 报告按分支范围组织：改了 N 页，每页是否工作 + 截图证据 + 相邻页面有无回归。

### Full（给了 URL 的默认模式）
系统性探索：访问每个可达页面，记录 5-10 个有充分证据的问题，产出健康评分。

### Quick（30 秒冒烟）
首页 + 顶部 5 个导航目标。查：加载？控制台报错？坏链？产出健康评分，不写详细问题。

## 工作流

### Phase 1-3：初始化、认证、定向
- 建输出目录（如 `<workspace>/qa-reports/screenshots/`）。
- 需登录：打开登录页→snapshot 找表单→填入（**密码写 [REDACTED]**）→提交→验证登录成功。2FA/CAPTCHA：请用户处理后再继续。
- 定向：打开目标页→snapshot+截图→梳理导航结构→记录落地页控制台报错。SPA 用 snapshot 找导航元素（链接列表可能为空）。

### Phase 4：探索（每页）
打开页面 → 截图存证 → 查控制台 → 逐页检查清单：
1. 视觉扫描（布局问题）
2. 交互元素（按钮/链接/控件可用？）
3. 表单（填+提交；测空、非法、边界值）
4. 导航（进出路径都测）
5. 状态（空态、加载、错误、溢出）
6. 控制台（交互后有无新 JS 报错）
7. 响应式（移动视口截图，如相关）

**深度判断**：核心功能（首页、仪表盘、结账、搜索）花更多时间，次要页（关于、条款）少花。

### Phase 5：记录（发现即记，不攒批）
- **交互类 bug**（流程断、按钮死、表单失败）：操作前截图 → 操作 → 结果截图 → 写复现步骤（引用截图）。
- **静态类 bug**（错别字、布局、缺图）：单张标注截图 + 描述。
- 密码一律写 `[REDACTED]`。

### Phase 6：收尾（基线评分）
按下方 rubric 算健康分，写"Top 3 待修项"，汇总控制台健康，存 `baseline.json`（日期/URL/分数/问题清单/分类分）供 regression 模式对比。

## 健康评分 Rubric（0-100 加权）

| 类别 | 权重 | 计分 |
|---|---|---|
| 控制台 | 15% | 0 错=100；1-3=70；4-10=40；10+=10 |
| 链接 | 10% | 0 坏=100；每个坏链 -15（最低 0） |
| 视觉/功能/UX/性能/内容/无障碍 | 其余 | 每项从 100 起：Critical -25、High -15、Medium -8、Low -3 |

`总分 = Σ(分类分 × 权重)`，各分类最低 0。

## Phase 7：分级（Triage）

按档位决定修哪些：
- **Quick**：critical+high，其余标 "deferred"
- **Standard**：+medium，low 标 deferred
- **Exhaustive**：全修

无法从源码修的（三方组件、基础设施）一律 deferred。

## Phase 8：修复循环（按严重级从高到低，逐条）

**8a 定位**：grep 报错串/组件名/路由定义，找负责源码。只改与问题直接相关的文件。
**8b 修复**：读源码理解上下文，做**最小修复**。不重构周边、不加功能、不"顺手改进"。
**8c 提交**：`git add <仅改动文件>` + `git commit -m "fix(qa): ISSUE-NNN — 简述"`。**一个修复一个提交，绝不打包。**
**8d 重测**：回受影响页 → before/after 截图对 → 查控制台 → 确认改动生效。
**8e 分类**：
- **verified**：重测确认修复生效、无新报错
- **best-effort**：已修但无法完全验证（需认证态/外部服务）
- **reverted**：发现回归 → `git revert HEAD` → 标 deferred

**8e.5 回归测试**（verified 且有测试框架时）：
1. 先读 2-3 个最近的测试文件，完全照搬项目惯例（命名/导入/断言风格）。
2. 追溯 bug 数据流：什么输入/状态触发？（前置条件）走哪条路径？在哪行断的？相邻边界用例一并测。
3. 测试必须：构造触发 bug 的前置条件 → 执行暴露 bug 的动作 → 断言正确行为（不是"能渲染""没抛错"）。附归因注释：`// Regression: ISSUE-NNN — 什么坏了 / 报告文件路径`。
4. 只跑新测试文件：过→单独提交 `test(qa): regression test for ISSUE-NNN`；挂→修一次，仍挂→删除并 defer。
5. 类型判断：控制台/JS 异常/逻辑 bug→单测或集成；表单/API/数据流→集成测试；带 JS 行为的视觉 bug→组件测试；纯 CSS→跳过。

**8f 自调节（每 5 个修复或任何 revert 后）**：

```
WTF-LIKELIHOOD: 从 0% 起
  每次 revert: +15%   每次修复动 >3 文件: +5%
  修到第 15 个后每多一个: +1%   剩余全是 Low: +10%
  触碰无关文件: +20%
WTF > 20% → 立即停止，展示已做工作，询问是否继续
硬上限：50 个修复后停止
```

## Phase 9：最终 QA

所有修复后重跑受影响页，算最终健康分。**若最终分比基线差：显著警告——有回归。**

## Phase 10：报告

输出报告（md 文件）：
- 摘要：发现 N 个问题，修复 M 个（verified X / best-effort Y / reverted Z），deferred 项
- 每个已修问题附：Fix Status、commit SHA、改动文件、before/after 截图
- 健康分变化：基线 → 最终
- PR 一句话总结：`QA found N issues, fixed M, health score X → Y.`

## Phase 11：TODOS.md 更新

新 deferred bug → 加 TODO（严重级/类别/复现步骤）；已修 bug → 标注 "Fixed by qa on <分支>, <日期>"。

## 重要规则

1. **复现至上**：每个问题至少一张截图，无例外。
2. **先验证再记录**：问题重试一次确认可复现，不是偶发。
3. **绝不写凭据**：复现步骤里密码写 `[REDACTED]`。
4. **增量写报告**：发现问题立刻追加，不攒批。
5. **不读源码来测试**：以用户视角，不当开发者。
6. **每次交互后查控制台**：不显形的 JS 报错也是 bug。
7. **像用户一样测**：用真实数据，走完整端到端流程。
8. **深度优于广度**：5-10 个有充分证据的问题 > 20 个模糊描述。
9. **从不删输出文件**：截图与报告累积是有意的。
10. **绝不拒绝开浏览器**：用户要 QA 就是要浏览器实测；diff 看似无 UI 改动也要测（后端改动影响行为）。
11. **一个修复一个提交**；只新建测试文件，不修改既有测试、不碰 CI 配置。
12. **回归即回滚**：修复变糟立刻 `git revert HEAD`。
13. **沉淀**：QA 中发现的非显然模式/坑按 post-task-self-review 惯例用 remember 保存。
