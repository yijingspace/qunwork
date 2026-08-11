use crate::model::BeeMessage;
use dashmap::DashMap;
use tokio::sync::{broadcast, mpsc};
use std::sync::Arc;

/// Agent 专属消息通道发送端。
pub type AgentSender = mpsc::Sender<BeeMessage>;
/// Agent 专属消息通道接收端。
pub type AgentReceiver = mpsc::Receiver<BeeMessage>;

/// 蜂群通信总线(内存版 8 字舞通信)。
///
/// 三层路由:
/// 1. 点对点: 按 `receiver` 精确投递到 Agent 通道;
/// 2. 组广播: 按 `task_group_id` 广播到临时项目蜂群;
/// 3. 全局广播: `receiver` 含 `"broadcast"` 时全集群广播。
#[derive(Clone)]
pub struct BeeBus {
    agent_channels: Arc<DashMap<String, AgentSender>>,
    group_broadcast: Arc<DashMap<String, broadcast::Sender<BeeMessage>>>,
    global_broadcast: broadcast::Sender<BeeMessage>,
}

impl BeeBus {
    pub fn new() -> Self {
        let (global_tx, _rx) = broadcast::channel(2048);
        Self {
            agent_channels: Arc::new(DashMap::new()),
            group_broadcast: Arc::new(DashMap::new()),
            global_broadcast: global_tx,
        }
    }

    /// Agent 注册消息通道, 返回该 Agent 的接收端(上层接入 `agent_run_loop`)。
    pub fn register_agent_channel(&self, agent_id: String) -> AgentReceiver {
        let (tx, rx) = mpsc::channel(512);
        self.agent_channels.insert(agent_id, tx);
        rx
    }

    /// 创建项目组广播通道。
    pub fn create_task_group_channel(&self, group_id: String) {
        let (tx, _) = broadcast::channel(1024);
        self.group_broadcast.insert(group_id, tx);
    }

    /// 发送消息核心路由: 点对点 + 组广播 + 全局广播标记。
    pub async fn send(&self, msg: BeeMessage) {
        for rid in &msg.receiver {
            if let Some(tx) = self.agent_channels.get(rid) {
                let _ = tx.send(msg.clone()).await;
            }
        }
        if let Some(group_tx) = self.group_broadcast.get(&msg.task_group_id) {
            let _ = group_tx.send(msg.clone());
        }
        if msg.receiver.contains(&"broadcast".to_string()) {
            let _ = self.global_broadcast.send(msg);
        }
    }

    /// 订阅某任务组的广播流。
    pub fn get_group_rx(&self, group_id: &str) -> Option<broadcast::Receiver<BeeMessage>> {
        self.group_broadcast.get(group_id).map(|v| v.subscribe())
    }

    /// 订阅全局广播流。
    pub fn get_global_rx(&self) -> broadcast::Receiver<BeeMessage> {
        self.global_broadcast.subscribe()
    }
}

impl Default for BeeBus {
    fn default() -> Self {
        Self::new()
    }
}
