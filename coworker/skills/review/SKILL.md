---
name: review
description: 合流前代码审查（PR/diff 审查）。分析当前分支相对基线的改动，按 CRITICAL/INFORMATIONAL 两类清单抓 CI 测不出的结构性问题（SQL 安全、并发竞态、LLM 信任边界、注入、枚举完整性等），自动修机械问题、批量询问需人工判断的问题。用于"review this PR / code review / check my diff / 审查代码 / 帮我看看改动"。
version: 1.0.0
category: dev-workflow
author: QunWork (蒸馏自 gstack/review, MIT)
tags: code-review, diff, pre-landing, sql-safety, race-condition
---

# 合流前代码审查（review）

## 何时使用

用户要求审查当前分支的改动、PR、diff，或即将合并/落地代码时主动建议使用。产出：发现清单（含置信度与修复建议）、已自动修复项、需用户决策项。

**铁律：审查前先读完整 diff；只报真问题；每个发现必须能引用具体代码行。**

## Step 0：确定基线与当前分支

1. 用 `git branch --show-current` 取当前分支；若在基线上或相对基线无改动，输出"无可审查内容"并停止。
2. 确定基线分支：`git remote get-url origin` 看平台（github/gitlab）；优先取 PR 目标分支（`gh pr view --json baseRefName -q .baseRefName` 或 `glab mr view -F json` 的 target_branch），失败则回退 `git symbolic-ref refs/remotes/origin/HEAD` → `origin/main` → `origin/master` → `main`。
3. 拉取最新基线避免陈旧状态误报：`git fetch origin <base> --quiet`，然后 `DIFF_BASE=$(git merge-base origin/<base> HEAD)`，用 `git diff "$DIFF_BASE"`（含已提交与未提交改动）和 `git log origin/<base>..HEAD --oneline` 作为审查输入。

## Step 1：范围漂移检测（审查任何代码前先做）

判断**改的是否正是该做的——不多不少**：

1. 读意图来源：commit message（`git log origin/<base>..HEAD --oneline`）、TODOS.md、PR 描述（如有）。
2. 用 `git diff "$DIFF_BASE" --stat` 对照意图，输出：

```
Scope Check: [CLEAN / DRIFT DETECTED / REQUIREMENTS MISSING]
Intent: <一句话说明要求做的事>
Delivered: <一句话说明 diff 实际做了什么>
[有漂移则逐条列出越界改动；有缺失则逐条列出未落实的需求]
```

范围检查是**信息性的**，不阻塞后续审查。

## Step 2：计划完成度审计（有计划文件/明确清单时）

若存在计划文件（如 docs/ 下设计文档、任务清单），提取其中的可执行项（复选框、编号步骤、"新建/修改 X"、测试要求、数据模型变更），逐项对照 diff 判定：

- **DONE** 有明确证据（引用具体文件）
- **PARTIAL** 部分实现
- **NOT DONE** 有负面证据（文件缺失、diff 无相关代码）
- **CHANGED** 换了实现方式但达成同一目标
- **UNVERIFIABLE** diff 无法证明（外部系统状态、跨仓文件）——诚实标注，宁可让用户手动确认，不要默认 DONE

对 PARTIAL/NOT DONE 项做轻量根因调查（commit 历史、相关代码），输出：

```
PLAN COMPLETION AUDIT
[DONE]      项1 — 证据文件
[NOT DONE]  项2 — 原因
[UNVERIFIABLE] 项3 — 需人工确认的外部系统
COMPLETION: X/N DONE ...
```

无计划文件时以 commit message 为意图来源，标注置信度较低。

## Step 3：核心审查——两遍清单

按 `git diff "$DIFF_BASE"` 逐文件审查。**每个发现必须带 file:line 引用触发它的代码原文**；引用不出具体代码行的发现视为未验证，不得进入主报告。

### Pass 1 —— CRITICAL（先做，最高严重级）

| 类别 | 检查点 |
|---|---|
| SQL 与数据安全 | 字符串插值拼 SQL（即使值做了 `.to_i`——一律用参数化查询）；check-then-set 的 TOCTOU 竞态（应原子 `WHERE`+`UPDATE`）；绕过模型校验的直接写库；循环/视图中关联未预加载的 N+1 |
| 竞态与并发 | 读-查-写无唯一约束或未捕获重复键错误；find-or-create 无唯一索引并发重复；状态迁移未用原子 `WHERE old_status=?`；对用户数据用 `.html_safe`/`dangerouslySetInnerHTML`/`v-html`/`|safe`（XSS） |
| LLM 输出信任边界 | LLM 生成值（邮箱/URL/名称）未经格式校验就写库或发邮件；结构化工具输出未做类型/形状校验；LLM 给的 URL 未过白名单就请求（SSRF，解析 hostname 查黑名单）；LLM 内容入库/向量库前未消毒（存储型提示注入） |
| 注入 | `subprocess` 系带 `shell=True` 且命令串做 f-string 插值（改用参数数组）；`os.system()` 带变量插值；对 LLM 生成代码执行 `eval`/`exec` 未沙箱 |
| 枚举与取值完整性 | diff 引入新枚举/状态/类型常量时，**必须读 diff 之外的代码**：grep 兄弟值的所有引用点，逐个读，确认每个 switch/过滤/展示/白名单数组都处理了新值；特别注意"前端下拉加了、后端模型没存"这类漏配 |

### Pass 2 —— INFORMATIONAL（低严重级但同样要处理）

| 类别 | 检查点 |
|---|---|
| 异步/同步混用 | async 函数内同步 `subprocess.run`/`requests.get`/`time.sleep` 阻塞事件循环（用 `asyncio.to_thread`/`aiofiles`/`httpx.AsyncClient`） |
| 列名/字段名安全 | ORM 查询的列名与实际 schema 核对，错列名会静默返回空结果或被吞错 |
| 版本/变更日志一致性 | PR 标题与 VERSION/CHANGELOG 不一致；CHANGELOG 描述与实际不符 |
| LLM 提示词问题 | 提示词里 0 起索引（LLM 通常按 1 起返回）；列出的工具/能力与实际接线不符；token 限制多处声明易漂移 |
| 完整性缺口 | 补全成本 <30 分钟却只做了快捷版；80-90% 实现但 100% 代价不大；缺镜像 happy-path 的负路径测试 |
| 时间窗安全 | 按"今天"键取数但报告窗口跨天；相关功能时间桶粒度不一致 |
| 边界类型强转 | 跨 JSON/序列化边界的数字↔字符串类型漂移（哈希/摘要输入必须归一化类型） |
| 前端 | 局部模板内联 `<style>`；视图里 O(n×m) 循环查找（改用 index_by 哈希）；本可下推 WHERE 的 Ruby 侧过滤 |
| CI/CD 流水线 | workflow 改动核对构建版本/产物路径/密钥用 `secrets.X` 而非硬编码；新产物类型有无发布流程；版本标签格式 `v1.2.3` vs `1.2.3` 全局一致；发布步骤幂等 |

### 置信度门（防误报，强制）

| 分数 | 含义 | 展示 |
|---|---|---|
| 9-10 | 读了具体代码，已证明是 bug/漏洞 | 正常展示 |
| 7-8 | 高置信模式匹配 | 正常展示 |
| 5-6 | 中等，可能是误报 | 标注"中等置信，请核实" |
| 3-4 | 低置信 | 只进附录，不进主报告 |
| 1-2 | 猜测 | 仅当严重级 P0 才报 |

**预发射验证门**：任何发现上报前必须引用触发它的具体代码行（file:line + 原文）。引不出 → 强制降到 4-5 分进附录。ORM 元类/迁移生成的符号，引用生成它的 meta 构造/迁移文件。

**不要报（抑制清单）**：无害且助读的冗余；"加注释解释阈值"（阈值常调，注释会腐）；断言已覆盖行为时的"可更严格"；纯一致性改动；受约束输入下永不发生的 regex 边界；同时覆盖多守卫的测试；经验调参的阈值；无害 no-op；**diff 内已处理的任何问题**。

## Step 4：Fix-First 处理

每个发现都得到处置，不止 critical：

1. **分类**：按 Fix-First 启发式——
   - **AUTO-FIX**（机械、资深工程师无需讨论就会改）：死代码/未用变量、缺预加载的 N+1、与代码矛盾的死注释、魔法数字改命名常量、缺 LLM 输出校验、版本/路径不一致、内联样式、O(n×m) 视图查找。
   - **ASK**（需要人工判断）：安全（auth/XSS/注入）、竞态、设计决策、大改动（>20 行）、枚举完整性、移除功能、任何改变用户可见行为的东西。
   - **规则**：critical 默认倾向 ASK，informational 默认倾向 AUTO-FIX。
2. **自动修**：所有 AUTO-FIX 项直接应用，每项输出一行 `[AUTO-FIXED] file:line 问题 → 处理`。
3. **批量问**：剩余 ASK 项用**一次** ask_user 列出（编号 + 严重级 + 问题 + 建议修复 + 推荐项），选项 A) 按建议修 B) 跳过。≤3 项时可逐个问。
4. **应用**：用户批准的项实施修复并输出。

**验证声明**：说"此模式安全"→引用证明安全的行；说"这里已处理"→读并引用处理代码；说"测试覆盖"→点名测试文件和用例。禁止"可能已处理""大概测过"。

## Step 5：收尾

1. **文档过时检查**：diff 改动的功能是否在 README/架构文档中有描述但文档未同步更新 → 报 INFORMATIONAL 项"文档可能过期"。
2. **TODOS 交叉**：PR 是否关闭了 TODO；是否应产生新 TODO。
3. **复盘沉淀**：审查中发现的非显然模式/坑 → 按 post-task-self-review 惯例沉淀：值得跨会话记住的用 remember 保存；可复用的检查模式可在本技能 checklist 中补充。

## 重要规则

- 读**完整** diff 再评论，不报 diff 内已解决的问题。
- Fix-first：AUTO-FIX 直接改；ASK 仅经批准后改。**不提交、不推送、不开 PR**——那是 ship 的职责。
- 简洁：一个问题一行，一个修复一行，不要铺垫。
- 只报真问题，跳过没问题的地方。
- 说"看起来没问题"不算发现——要么引用证据说明没问题，要么标记为未验证。
