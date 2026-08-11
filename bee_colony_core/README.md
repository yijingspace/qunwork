# bee_colony_core — 蜂群通信总线 + Agent 调度核心（研究基线）

> 定位：**研究基线，不接生产**（同 `coworker/periodic/dpnn_baseline.py`）。
> 来源：《企业AI蜂群Agent组织架构全案》Rust 最小原型（2026-08-12 入库）。
> 生产实现：QunWork 侧为 Python 版 orchestrator + HORNET（控制面 event_sink + 协调面信息素场 `coworker/pheromone.py`），本 crate 独立演进，验证蜂群通信/调度概念。

## 模块

| 模块 | 职责 |
|---|---|
| `src/model.rs` | 消息数据模型：`BeeMessage`（msg_id/sender/receiver/task_group_id/msg_type/priority/payload/timestamp/trace_id），对齐方案通信协议 |
| `src/bus.rs` | 内存通信总线（8 字舞）：点对点路由 + 组广播 + 全局广播；进程内异步，分布式可替换为 Redis-Stream/Kafka |
| `src/agent.rs` | `BeeAgent` trait 抽象 + `agent_run_loop` 通用运行循环（Idle/Working/Fault 状态） |
| `src/scheduler.rs` | 蜂群调度核心：Agent 注册、动态组网（spawn_task_group）、解散回收（dissolve_task_group）、负载探查（get_agent_load） |

## 运行

```bash
cd bee_colony_core
cargo run          # 注册 5 Agent → 组建项目蜂群 → 打印成员负载
cargo build        # 编译验证
```

## 依赖

Rust ≥ 1.75；`tokio`（async 运行时）、`uuid`、`serde/serde_json`、`dashmap`（并发 map）、`chrono`。

## 原型边界与扩展路线（方案原文）

1. 总线为内存异步消息，进程内可用；分布式部署把消息层替换为 Redis-Stream/Kafka；
2. Agent 基类仅定义 trait，业务 Agent（董事长/会计/研发等）需实现 `BeeAgent`；
3. 尚未接入全局记忆库 RAG、纠错 Agent 校验、算力配额模块。
4. 扩展路线：业务 Agent 样例 / 分布式总线 / 心跳故障迁移 + 算力配额。
