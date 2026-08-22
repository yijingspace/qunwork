---
name: context-save
description: 保存当前工作上下文（进度检查点）。把当前目标、已做决策、剩余工作、注意事项和改动文件清单写成结构化文件，供 context-restore 恢复。用于"context-save / 保存进度 / checkpoint / 记住我做到哪了"。
version: 1.0.0
category: dev-workflow
author: QunWork (蒸馏自 gstack/context-save, MIT)
tags: context, checkpoint, save, resume
---

# 保存工作上下文（context-save）

## 何时使用

用户要求保存进度、切换任务前、会话可能中断时，或"记住我做到哪了"。

**绝不修改代码**——只读状态、写上下文文件。

## 保存流程

1. **收集状态**：
   - 当前分支：`git branch --show-current`
   - 改动文件：`git status --short`（已暂存+未暂存），用相对路径
   - 会话时长：可估算则记，未知省略

2. **推断标题**（不盘问）——从 git 状态与会话上下文推断；确实无法推断才 ask_user。

3. **写文件**：`<工作区>/.context/checkpoints/<时间戳>-<标题slug>.md`
   - 时间戳 `YYYYMMDD-HHMMSS`；标题 slug 只留 `a-z0-9-`，小写、压缩空白、截断 60 字符
   - **append-only，绝不覆盖**——同名文件加随机后缀

4. **文件格式**：

```markdown
---
status: in-progress
branch: {当前分支}
timestamp: {ISO-8601}
session_duration_s: {时长，未知省略}
files_modified:
  - path/to/file1
---

## Working on: {标题}

### Summary
{1-3 句：高层目标与当前进展}

### Decisions Made
{架构选择、权衡、理由的要点列表}

### Remaining Work
{具体下一步，按优先级编号}

### Notes
{坑、阻塞项、开放问题、试过但没用的方法}
```

5. **确认输出**：

```
CONTEXT SAVED
════════════════════════════════
Title:    {标题}
Branch:   {分支}
File:     {文件路径}
Modified: {N} files
Duration: {时长或 unknown}
════════════════════════════════
之后可用 context-restore 恢复。
```

## 列表流程

- 列出 `<工作区>/.context/checkpoints/` 下按文件名（时间戳前缀）倒序的文件。
- **默认只显示当前分支的**；`--all` 显示全部分支（加 Branch 列）。
- 从 frontmatter 提取 status/branch/timestamp，标题从文件名解析（时间戳之后部分）。
- 表格展示：# / 日期 / 标题 / 状态（或 Branch）。
- 无保存记录 → "还没有保存的上下文。运行 context-save 保存当前工作状态。"

## 重要规则

- 绝不改代码；只读状态写文件。
- frontmatter 必须含分支名——跨分支恢复的关键。
- 保存文件 append-only，绝不覆盖或删除；每次保存新建文件。
- 推断而非盘问：能用 git 状态和会话上下文填的就不问。
