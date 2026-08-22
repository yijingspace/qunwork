---
name: context-restore
description: 恢复之前保存的工作上下文（进度检查点）。读取 context-save 保存的文件，呈现摘要、剩余工作与注意事项，接着上次的进度继续。用于"context-restore / 恢复进度 / 我上次做到哪了 / 接着干"。
version: 1.0.0
category: dev-workflow
author: QunWork (蒸馏自 gstack/context-restore, MIT)
tags: context, checkpoint, restore, resume
---

# 恢复工作上下文（context-restore）

## 何时使用

用户要求恢复进度、"我上次做到哪了"、跨会话续接工作时。

**绝不修改代码**——只读保存的文件并呈现。

## 模式解析

- `context-restore` → 加载最近的保存上下文（当前分支优先，其次任意分支）
- `context-restore <标题片段或编号>` → 加载指定的保存上下文
- `context-restore list` → 提示"列表在 context-save 侧：运行 context-save list"，不在此处列表

## 恢复流程

### Step 1：查找保存的上下文

扫描 `<工作区>/.context/checkpoints/*.md`：
- 排序以**文件名时间戳前缀**（`YYYYMMDD-HHMMSS`）为准，不是文件系统 mtime。
- **当前分支的优先**（从每个文件 frontmatter 的 branch 字段读），其他分支作为后备——当前分支没有检查点时可跨分支恢复。
- 最多列 20 个（防止海量文件撑爆上下文）。

### Step 2：加载正确的文件

- 用户给了标题片段/编号 → 在候选中匹配。
- 否则加载第一个（当前分支最新，或全分支最新）。

读文件并呈现摘要：

```
RESUMING CONTEXT
════════════════════════════════
Title:       {标题}
Branch:      {frontmatter 里的分支}
Saved:       {时间戳，人类可读}
Duration:    {上次会话时长（如有）}
Status:      {状态}
════════════════════════════════

### Summary
{保存文件里的摘要}

### Remaining Work
{剩余工作项}

### Notes
{注意事项}
```

**分支不一致提示**：若当前分支与保存的分支不同，注明："此上下文保存于分支 {saved}。你当前在 {current}。继续前可能需要切换分支。"

### Step 3：提供下一步

呈现后用 ask_user：
- A) 继续做剩余工作项（推荐）
- B) 显示完整保存文件
- C) 只要上下文，谢谢

选 A → 概括第一个剩余工作项并建议从那里开始。

## 无保存记录时

提示："还没有保存的上下文。先运行 context-save 保存当前工作状态，之后 context-restore 就能找到。"

## 重要规则

- 绝不改代码；只读文件并呈现。
- 优先当前分支自己的检查点，但保留全分支后备集（跨分支恢复仍可用）。
- "最近"= 文件名 `YYYYMMDD-HHMMSS` 前缀，不是 mtime。
- 加载后引导用户从剩余工作第一项继续，形成闭环。
