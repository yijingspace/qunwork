use crate::agent::AgentState;
use crate::bus::BeeBus;
use crate::model::{BeeMessage, MsgType};
use dashmap::DashMap;
use serde_json::json;
use std::sync::Arc;
use uuid::Uuid;

/// 临时项目蜂群(任务组) — 组网/解散生命周期的元数据。
#[derive(Debug, Clone)]
pub struct TaskGroup {
    pub group_id: String,
    pub owner_agent: String,
    pub member_ids: Vec<String>,
    pub is_active: bool,
}

/// 蜂群调度核心 — Agent 注册、动态组网、解散回收、负载探查。
pub struct ColonyScheduler {
    bus: Arc<BeeBus>,
    agent_state: DashMap<String, AgentState>,
    task_groups: DashMap<String, TaskGroup>,
}

impl ColonyScheduler {
    pub fn new(bus: BeeBus) -> Self {
        Self {
            bus: Arc::new(bus),
            agent_state: DashMap::new(),
            task_groups: DashMap::new(),
        }
    }

    /// 注册 Agent: 记 Idle 状态 + 自动创建专属消息通道。
    pub async fn register_agent(&mut self, agent_id: String) {
        self.agent_state.insert(agent_id.clone(), AgentState::Idle);
        let _rx = self.bus.register_agent_channel(agent_id);
    }

    /// 动态创建项目蜂群: 建组广播通道 + 存元数据 + 下发入组指令 + 置工作态。
    pub async fn spawn_task_group(
        &mut self,
        group_id: String,
        owner_agent: String,
        member_list: Vec<&str>,
    ) {
        let member_ids: Vec<String> = member_list.iter().map(|s| s.to_string()).collect();
        self.bus.create_task_group_channel(group_id.clone());
        let tg = TaskGroup {
            group_id: group_id.clone(),
            owner_agent,
            member_ids: member_ids.clone(),
            is_active: true,
        };
        self.task_groups.insert(group_id.clone(), tg);

        let trace_id = Uuid::new_v4().to_string();
        let msg = BeeMessage::new(
            "scheduler".into(),
            member_ids,
            group_id.clone(),
            MsgType::Command,
            3,
            json!({"action": "join_group", "group_id": group_id}),
            trace_id,
        );
        self.bus.send(msg).await;

        for mid in member_list {
            self.agent_state.insert(mid.into(), AgentState::Working(group_id.clone()));
        }
    }

    /// 解散蜂群, 回收资源: 通知成员退出 + Agent 切回空闲 + 移除组元数据。
    pub async fn dissolve_task_group(&mut self, group_id: &str) {
        let member_ids: Vec<String> = if let Some(tg) = self.task_groups.get(group_id) {
            let ids = tg.member_ids.clone();
            self.task_groups.insert(
                group_id.to_string(),
                TaskGroup {
                    group_id: tg.group_id.clone(),
                    owner_agent: tg.owner_agent.clone(),
                    member_ids: ids.clone(),
                    is_active: false,
                },
            );
            ids
        } else {
            return;
        };

        let trace_id = Uuid::new_v4().to_string();
        let msg = BeeMessage::new(
            "scheduler".into(),
            member_ids.clone(),
            group_id.to_string(),
            MsgType::Command,
            2,
            json!({"action": "leave_group", "group_id": group_id}),
            trace_id,
        );
        self.bus.send(msg).await;

        for mid in &member_ids {
            self.agent_state.insert(mid.clone(), AgentState::Idle);
        }
        self.task_groups.remove(group_id);
    }

    /// 查询 Agent 负载状态, 用于资源探查。
    pub fn get_agent_load(&self) -> Vec<(String, AgentState)> {
        self.agent_state
            .iter()
            .map(|e| (e.key().clone(), e.value().clone()))
            .collect()
    }
}
