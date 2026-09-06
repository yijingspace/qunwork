---
name: oir-longrun
description: 发起/查看/控制 OIR 长程概念索引任务（QunWork ↔ OIR 握手网关）。Use when the user says "发起长程任务", "长程索引", "索引研究文档", "oir longrun", or wants to start / check / pause / resume / complete a long-running concept-indexing task.
argument-hint: "[目标描述，例如：索引本月新增的研究文档]"
allowed-tools: Bash(*), Read, Glob
---

# OIR 长程任务（用户发起入口）

用户目标: $ARGUMENTS

你负责把用户的意图变成一个**真实的长程任务**提交到 OIR 握手网关。QunWork
7×24 调度器（每 30s 一 tick）会自动收养并驱动网关上的所有任务——你只需
提交成功并回报 goal_id，无需自己推进。

## 固定契约

- 网关: `http://127.0.0.1:8787`（仅本机回环，无鉴权）
- 提交: `POST /api/longrun/submit`，body `{"goal_id","goal","total_documents"}`
- 状态: `GET /api/longrun/task?goal_id=<gid>`
- 控制: `POST /api/longrun/pause|resume|complete`，body `{"goal_id":"<gid>"}`
- goal_id 约定: `qunwork-manual-<YYYYMMDD-HHMMSS>`（时间戳取当前本地时间）

## 工作流

### Step 1: 握手检查
```bash
curl -s http://127.0.0.1:8787/api/longrun/health
```
要求 `success:true` 且 `data.horizon_ready:true`。否则**停止**并告诉用户：
先启动网关（计划任务 "OIR Longrun Gateway"，或
`pwsh E:\DesktopProjects\OIR\oir-rebuilt\scripts\start_oir_longrun_gateway.ps1`）。

### Step 2: 统计文档总量
用 Glob 统计 QunWork 工作区（默认 `E:\QunWork\研究文档`）下 `*.md` 数量，
作为 `total_documents`（0 或取不到时用 1，OIR 侧按持续增长记账）。

### Step 3: 提交任务
```bash
curl -s -X POST http://127.0.0.1:8787/api/longrun/submit \
  -H "Content-Type: application/json" \
  -d '{"goal_id":"qunwork-manual-<时间戳>","goal":"<用户目标原文>","total_documents":<N>}'
```
Windows 下引号转义困难时改用 python（UTF-8 安全）：
```bash
python -X utf8 -c "import json,urllib.request; d={'goal_id':'qunwork-manual-<时间戳>','goal':'''<用户目标>''','total_documents':<N>}; print(urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8787/api/longrun/submit', json.dumps(d).encode('utf-8'), {'Content-Type':'application/json'})).read().decode('utf-8'))"
```
要求响应 `success:true`（`goal_id 已存在` → 换一个时间戳重试一次）。

### Step 4: 回报用户
- 已提交: goal_id、文档总量、oir_task_id
- 说明: QunWork 调度器最迟 ~30s 开始自动推进（读 `doc_dir` 下的文档逐批
  索引），交付物写入 `E:\QunWork\OIR握手交付\longrun\`
- 进度查看: 7×24 管理页 → OIR longrun 面板（或用 Step 5 的状态接口）

### Step 5: 查询/控制（用户追问进度或要求暂停/完成时）
```bash
curl -s "http://127.0.0.1:8787/api/longrun/task?goal_id=<gid>"
curl -s -X POST http://127.0.0.1:8787/api/longrun/pause   -H "Content-Type: application/json" -d '{"goal_id":"<gid>"}'
curl -s -X POST http://127.0.0.1:8787/api/longrun/resume  -H "Content-Type: application/json" -d '{"goal_id":"<gid>"}'
curl -s -X POST http://127.0.0.1:8787/api/longrun/complete -H "Content-Type: application/json" -d '{"goal_id":"<gid>"}'
```
`phase` 取值: Planning / Executing（进行中）、Paused（已暂停）、
Completed（已完成）。
