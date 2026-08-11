// 研究基线: 完整 API 面保留, 示例仅演示核心路径 — 允许未用的公共 API。
#![allow(dead_code)]

mod agent;
mod bus;
mod model;
mod scheduler;

use bus::BeeBus;
use scheduler::ColonyScheduler;
use uuid::Uuid;

/// 研究基线示例: 董事长下发战略目标 → 动态组建项目蜂群。
///
/// 运行: `cargo run`(观察入组指令经总线投递给各成员)。
#[tokio::main]
async fn main() {
    // 初始化内存通信总线
    let bus = BeeBus::new();
    // 初始化蜂群调度器
    let mut scheduler = ColonyScheduler::new(bus.clone());

    // 注册示例 Agent
    for id in ["chairman_01", "gm_01", "pm_bee_01", "dev_bee_01", "audit_bee_01"] {
        scheduler.register_agent(id.into()).await;
    }

    // 模拟: 董事长下发战略目标
    let task_group_id = Uuid::new_v4().to_string();
    scheduler
        .spawn_task_group(
            task_group_id.clone(),
            "chairman_01".into(),
            vec!["gm_01", "pm_bee_01", "dev_bee_01", "audit_bee_01"],
        )
        .await;

    println!("[bee_colony_core] 项目蜂群已创建 group_id={}", task_group_id);
    println!(
        "[bee_colony_core] 成员负载: {:?}",
        scheduler.get_agent_load()
    );

    tokio::time::sleep(tokio::time::Duration::from_secs(2)).await;
    println!("[bee_colony_core] 示例结束(研究基线, 不接生产)");
}
