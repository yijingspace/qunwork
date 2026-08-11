use crate::bus::{AgentReceiver, BeeBus};
use crate::model::BeeMessage;
use std::sync::Arc;

/// Agent 状态 — 调度器的忙闲信号(Idle/Working/Fault)。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum AgentState {
    Idle,
    /// 携带当前 task_group_id
    Working(String),
    Fault,
}

/// Agent 抽象 — 业务 Agent(董事长/总经理/会计等)实现此 trait。
pub trait BeeAgent {
    fn agent_id(&self) -> &str;
    fn state(&self) -> AgentState;
    fn on_message(&mut self, msg: BeeMessage, bus: Arc<BeeBus>);
}

/// 通用运行循环 — 阻塞消费该 Agent 的专属通道, 转发给 `on_message`。
pub async fn agent_run_loop(mut agent: impl BeeAgent, bus: Arc<BeeBus>, mut rx: AgentReceiver) {
    while let Some(msg) = rx.recv().await {
        agent.on_message(msg, bus.clone());
    }
}
