use serde::{Deserialize, Serialize};
use uuid::Uuid;
use chrono::{DateTime, Utc};

/// 消息类型 — 与方案通信协议一致(command/request/feedback/alert/broadcast)。
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MsgType {
    Command,
    Request,
    Feedback,
    Alert,
    Broadcast,
}

/// 蜂群消息 — sender/receiver/task_group_id/priority/trace_id 全字段对齐方案协议。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BeeMessage {
    pub msg_id: String,
    pub sender: String,
    pub receiver: Vec<String>,
    pub task_group_id: String,
    pub msg_type: MsgType,
    /// 0~5, 5 最高(宕机/重大风险)
    pub priority: u8,
    pub payload: serde_json::Value,
    pub timestamp: DateTime<Utc>,
    pub trace_id: String,
}

impl BeeMessage {
    pub fn new(
        sender: String,
        receiver: Vec<String>,
        task_group_id: String,
        msg_type: MsgType,
        priority: u8,
        payload: serde_json::Value,
        trace_id: String,
    ) -> Self {
        Self {
            msg_id: Uuid::new_v4().to_string(),
            sender,
            receiver,
            task_group_id,
            msg_type,
            priority,
            payload,
            timestamp: Utc::now(),
            trace_id,
        }
    }
}
