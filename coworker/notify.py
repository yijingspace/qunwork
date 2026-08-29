"""7x24 告警多渠道通知 — 邮件 / Telegram / 飞书 / 钉钉 / 微信 webhook.

与突破方案四 (蜂巢心跳) 配套: 心跳卡死等 7x24 告警除落库 (AlertStore) 与
桌面端推送外, 可按配置的渠道把告警转发到外部 (邮件 SMTP / Telegram Bot /
飞书自定义机器人 / 钉钉自定义机器人 / 企业微信机器人 webhook), 实现
"卡死时即使不在桌面端也能收到提醒"。

渠道配置格式 (JSON, 存于 secrets/配置):
{
  "channels": {
    "email":   {"enabled": true, "smtp_host": "...", "smtp_port": 465,
                "username": "...", "password": "...", "to": ["a@x.com"],
                "use_tls": true, "from": "qunwork@x.com"},
    "telegram":{"enabled": true, "bot_token": "...", "chat_id": "..."},
    "feishu":  {"enabled": true, "webhook_url": "https://open.feishu.cn/...",
                "secret": ""},
    "dingtalk":{"enabled": true, "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=...",
                "secret": ""},
    "wecom":   {"enabled": true, "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..."}
  }
}

所有发送都是 best-effort: 任一渠道失败记日志, 不影响告警主流程。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import smtplib
import time
from email.mime.text import MIMEText
from typing import Any, Optional
from urllib import request

logger = logging.getLogger(__name__)

CHANNEL_KEYS = ("email", "telegram", "feishu", "dingtalk", "wecom")

# 告警级别: critical > warning > info。渠道可配置订阅哪些级别。
ALERT_LEVELS = ("critical", "warning", "info")

# 默认渠道配置 (全部禁用; 用户按需开启)。levels=全部级别 (空=订阅所有)。
DEFAULT_CHANNELS: dict[str, Any] = {
    "email": {"enabled": False, "levels": []},
    "telegram": {"enabled": False, "levels": []},
    "feishu": {"enabled": False, "levels": []},
    "dingtalk": {"enabled": False, "levels": []},
    "wecom": {"enabled": False, "levels": []},
}


def _sign_webhook(secret: str, timestamp: str) -> str:
    """飞书/钉钉机器人签名 (HMAC-SHA256, base64)。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    import base64

    return base64.b64encode(hmac_code).decode("utf-8")


class AlertNotifier:
    """多渠道告警通知器。

    用法::

        notifier = AlertNotifier(config)   # config = {"channels": {...}}
        await notifier.send("任务 t1 心跳停滞", title="7x24 告警", task_id="t1")
    """

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        channels = (config or {}).get("channels") or {}
        self.channels: dict[str, Any] = {}
        for key in CHANNEL_KEYS:
            merged = dict(DEFAULT_CHANNELS[key])
            merged.update(channels.get(key) or {})
            self.channels[key] = merged

    @property
    def enabled_channels(self) -> list[str]:
        return [k for k, v in self.channels.items() if v.get("enabled")]

    async def send(
        self,
        message: str,
        *,
        title: str = "7×24 告警",
        task_id: Optional[str] = None,
        level: str = "warning",
    ) -> dict[str, Any]:
        """按启用渠道发送告警, 返回各渠道结果 {channel: ok/error}。

        ``level`` (critical/warning/info): 只发给订阅了该级别 (或未限定级别)
        的渠道 — 实现"不同告警种类走不同渠道"的按级别路由。
        """
        level = level if level in ALERT_LEVELS else "warning"
        results: dict[str, Any] = {}
        for channel in self.enabled_channels:
            cfg = self.channels[channel]
            levels = cfg.get("levels") or []
            # 渠道订阅级别为空 → 订阅所有; 否则必须包含本次级别。
            if levels and level not in levels:
                results[channel] = {"ok": None, "skipped": True}
                continue
            try:
                ok = await self._dispatch(channel, message, title=title, task_id=task_id)
                results[channel] = {"ok": ok}
            except Exception as exc:  # best-effort: 单渠道失败不阻断其余
                logger.warning("alert channel %s failed: %s", channel, exc)
                results[channel] = {"ok": False, "error": str(exc)}
        return results

    async def _dispatch(
        self, channel: str, message: str, *, title: str, task_id: Optional[str]
    ) -> bool:
        cfg = self.channels[channel]
        if channel == "email":
            return await asyncio.to_thread(self._send_email, cfg, title, message)
        if channel == "telegram":
            return await asyncio.to_thread(self._send_telegram, cfg, message)
        if channel == "feishu":
            return await asyncio.to_thread(self._send_feishu, cfg, message)
        if channel == "dingtalk":
            return await asyncio.to_thread(self._send_dingtalk, cfg, message)
        if channel == "wecom":
            return await asyncio.to_thread(self._send_wecom, cfg, message)
        return False

    # -- ① 渠道健康探针 -----------------------------------------------------
    async def probe(self) -> dict[str, Any]:
        """健康探针: 向每个**启用且配置完整**的渠道发一条测试消息。

        返回 {channel: {"ok", "ms", "error"}} — ms 为发送耗时 (毫秒)。
        与 ``send`` 的区别: probe 不按级别过滤 (渠道订阅级别为空才视为
        可测), 且返回延迟; 配置不完整的渠道返回 ok=False + 原因。
        """
        import time as _t

        results: dict[str, Any] = {}
        for channel in self.enabled_channels:
            cfg = self.channels[channel]
            # 配置完整性检查 (webhook 类必须有 url, telegram 需 token+chat_id)。
            if channel == "telegram" and not (cfg.get("bot_token") and cfg.get("chat_id")):
                results[channel] = {"ok": False, "error": "incomplete config (bot_token/chat_id)"}
                continue
            if channel in ("feishu", "dingtalk", "wecom") and not cfg.get("webhook_url"):
                results[channel] = {"ok": False, "error": "incomplete config (webhook_url)"}
                continue
            if channel == "email" and not (cfg.get("smtp_host") and cfg.get("to")):
                results[channel] = {"ok": False, "error": "incomplete config (smtp_host/to)"}
                continue
            t0 = _t.perf_counter()
            try:
                ok = await self._dispatch(
                    channel, "✅ 7×24 渠道健康探针 — 来自 QunWork", title="渠道健康探针"
                )
                results[channel] = {
                    "ok": ok,
                    "ms": round((_t.perf_counter() - t0) * 1000, 1),
                }
            except Exception as exc:
                results[channel] = {
                    "ok": False,
                    "ms": round((_t.perf_counter() - t0) * 1000, 1),
                    "error": str(exc),
                }
        return results

    # -- 邮件 --------------------------------------------------------------
    def _send_email(self, cfg: dict, title: str, body: str) -> bool:
        host = cfg.get("smtp_host")
        port = int(cfg.get("smtp_port") or 465)
        username = cfg.get("username")
        password = cfg.get("password")
        to = cfg.get("to") or []
        from_addr = cfg.get("from") or username
        if not host or not username or not to:
            logger.warning("email channel incomplete config (host/user/to)")
            return False
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = title
        msg["From"] = from_addr
        msg["To"] = ", ".join(to)
        use_tls = bool(cfg.get("use_tls", True))
        if use_tls:
            with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                if username and password:
                    s.login(username, password)
                s.sendmail(from_addr, to, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                s.starttls()
                if username and password:
                    s.login(username, password)
                s.sendmail(from_addr, to, msg.as_string())
        return True

    # -- Telegram -----------------------------------------------------------
    def _send_telegram(self, cfg: dict, message: str) -> bool:
        token = cfg.get("bot_token")
        chat_id = cfg.get("chat_id")
        if not token or not chat_id:
            logger.warning("telegram channel incomplete config (token/chat_id)")
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": message}).encode("utf-8")
        req = request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        with request.urlopen(req, timeout=15) as resp:
            return resp.status == 200

    # -- 飞书 (自定义机器人) ------------------------------------------------
    def _send_feishu(self, cfg: dict, message: str) -> bool:
        url = cfg.get("webhook_url")
        if not url:
            logger.warning("feishu channel missing webhook_url")
            return False
        timestamp = str(int(time.time()))
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": message},
        }
        if cfg.get("secret"):
            payload["timestamp"] = timestamp
            payload["sign"] = _sign_webhook(cfg["secret"], timestamp)
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=15) as resp:
            return resp.status == 200

    # -- 钉钉 (自定义机器人) ------------------------------------------------
    def _send_dingtalk(self, cfg: dict, message: str) -> bool:
        url = cfg.get("webhook_url")
        if not url:
            logger.warning("dingtalk channel missing webhook_url")
            return False
        payload: dict[str, Any] = {
            "msgtype": "text",
            "text": {"content": message},
        }
        if cfg.get("secret"):
            timestamp = str(int(time.time() * 1000))  # 钉钉用毫秒
            sign = _sign_webhook(cfg["secret"], timestamp)
            payload["timestamp"] = timestamp
            payload["sign"] = sign
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=15) as resp:
            return resp.status == 200

    # -- 企业微信 (群机器人) ------------------------------------------------
    def _send_wecom(self, cfg: dict, message: str) -> bool:
        url = cfg.get("webhook_url")
        if not url:
            logger.warning("wecom channel missing webhook_url")
            return False
        payload = {"msgtype": "text", "text": {"content": message}}
        req = request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with request.urlopen(req, timeout=15) as resp:
            return resp.status == 200

    # -- 配置管理 ------------------------------------------------------------
    def public_config(self) -> dict[str, Any]:
        """脱敏配置 (密码/密钥打码), 供 GUI 展示。"""
        out: dict[str, Any] = {}
        for key in CHANNEL_KEYS:
            cfg = dict(self.channels[key])
            for field in ("password", "bot_token", "secret"):
                if cfg.get(field):
                    cfg[field] = "••••"
            out[key] = cfg
        return out
