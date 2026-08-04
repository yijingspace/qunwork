"""One-shot i18n batch: wrap high-frequency UI strings in t() and append zh dict.

Reads a replacement map, applies exact-string replacements per file, then merges
new keys into messages.ts (before the closing `};`). Verify with tsc + vitest.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "surfaces" / "gui" / "src"

# (file, [(exact_string, t_key)]) — t_key == English original (the i18n convention).
REPLACEMENTS: dict[str, list[tuple[str, str]]] = {
    "components/App.tsx": [
        ('"Try a task"', 't("Try a task")'),
        ('"Waiting for agent..."', 't("Waiting for agent...")'),
        ('label="Try a task"', 'label={t("Try a task")}'),
    ],
    "components/Composer.tsx": [
        ('"Transcribing…"', 't("Transcribing…")'),
        ('"Loading models…"', 't("Loading models…")'),
        ('"Send approvals to Inbox"', 't("Send approvals to Inbox")'),
        ('placeholder="Send a message…"', 'placeholder={t("Send a message…")}'),
    ],
    "components/Transcript.tsx": [
        ('title="Copy message"', 'title={t("Copy message")}'),
        ('"✕ declined"', 't("✕ declined")'),
        ('"assistant"', 't("assistant")'),
        ('"proposed plan"', 't("proposed plan")'),
    ],
    "components/RightRail.tsx": [
        ('title="Progress"', 'title={t("Progress")}'),
        ('title="Refresh artifacts"', 'title={t("Refresh artifacts")}'),
        ('"No previewable files yet."', 't("No previewable files yet.")'),
        ('"Open"', 't("Open")'),
        ('aria-label="Back to artifacts"', 'aria-label={t("Back to artifacts")}'),
        ('title="Back"', 'title={t("Back")}'),
        ('"Artifacts"', 't("Artifacts")'),
        ('"Reload preview"', 't("Reload preview")'),
        ('"Reload"', 't("Reload")'),
        ('"Open in default app"', 't("Open in default app")'),
        ('"Copy path"', 't("Copy path")'),
        ('"Copy full path"', 't("Copy full path")'),
        ('"Show in folder"', 't("Show in folder")'),
        ('"Loading..."', 't("Loading...")'),
        ('"Empty file."', 't("Empty file.")'),
        ('"Empty sheet."', 't("Empty sheet.")'),
    ],
    "components/SearchModal.tsx": [
        ('"No chats found."', 't("No chats found.")'),
    ],
    "components/InboxView.tsx": [
        ('title="Inbox"', 'title={t("Inbox")}'),
        ('"Delivered here only."', 't("Delivered here only.")'),
    ],
    "components/AuditView.tsx": [
        ('title="Activity"', 'title={t("Activity")}'),
        ('"No audit events yet."', 't("No audit events yet.")'),
    ],
    "components/IntegrationsView.tsx": [
        ('title="Connectors"', 'title={t("Connectors")}'),
        ('title="MCP servers"', 'title={t("MCP servers")}'),
    ],
    "components/PersonaView.tsx": [
        ('"Back"', 't("Back")'),
        ('"Persona"', 't("Persona")'),
        ('title="Enable this persona"', 'title={t("Enable this persona")}'),
        ('"About"', 't("About")'),
        ('"Built-in capabilities"', 't("Built-in capabilities")'),
        ('"Models"', 't("Models")'),
        ('"Default mode"', 't("Default mode")'),
    ],
    "components/Sidebar.tsx": [
        ('"Delete?"', 't("Delete?")'),
        ('"Delete"', 't("Delete")'),
    ],
    "components/ConnectorsList.tsx": [
        ('placeholder="Search connectors…"', 'placeholder={t("Search connectors…")}'),
        ('"Available"', 't("Available")'),
        ('"Nothing matches."', 't("Nothing matches.")'),
    ],
    "components/SwarmView.tsx": [
        ('`${m}分${s}秒`', '`${m}${t("m")}${s}${t("s")}`'),
    ],
}

# New dict entries: key (English) -> zh.
DICT: dict[str, str] = {
    "Try a task": "试试一个任务",
    "Waiting for agent...": "等待智能体…",
    "Transcribing…": "转写中…",
    "Loading models…": "加载模型…",
    "Send approvals to Inbox": "审批发送到收件箱",
    "Send a message…": "发送消息…",
    "Copy message": "复制消息",
    "✕ declined": "✕ 已拒绝",
    "assistant": "智能体",
    "proposed plan": "建议计划",
    "Progress": "进度",
    "Refresh artifacts": "刷新产物",
    "No previewable files yet.": "还没有可预览的文件。",
    "Open": "打开",
    "Back to artifacts": "返回产物",
    "Back": "返回",
    "Artifacts": "产物",
    "Reload preview": "重新加载预览",
    "Reload": "重新加载",
    "Open in default app": "用默认应用打开",
    "Copy path": "复制路径",
    "Copy full path": "复制完整路径",
    "Show in folder": "在文件夹中显示",
    "Loading...": "加载中…",
    "Empty file.": "空文件。",
    "Empty sheet.": "空工作表。",
    "No chats found.": "没有找到会话。",
    "Inbox": "收件箱",
    "Delivered here only.": "只在这里投递。",
    "Activity": "活动",
    "No audit events yet.": "还没有活动记录。",
    "Connectors": "连接器",
    "MCP servers": "MCP 服务器",
    "Persona": "角色",
    "Enable this persona": "启用此角色",
    "About": "关于",
    "Built-in capabilities": "内置能力",
    "Models": "模型",
    "Default mode": "默认模式",
    "Delete?": "删除?",
    "Delete": "删除",
    "Search connectors…": "搜索连接器…",
    "Available": "可用",
    "Nothing matches.": "没有匹配项。",
    "m": "分",
    "s": "秒",
}


def main() -> None:
    changed = []
    for rel, pairs in REPLACEMENTS.items():
        p = ROOT / rel
        if not p.exists():
            print(f"!! missing {rel}")
            continue
        text = p.read_text(encoding="utf-8")
        orig = text
        for old, new in pairs:
            if old not in text:
                print(f"   skip {rel}: {old!r} not found")
                continue
            text = text.replace(old, new, 1)
        if text != orig:
            p.write_text(text, encoding="utf-8")
            changed.append(rel)
            print(f"✓ {rel} ({len(pairs)} pairs)")

    # merge dict into messages.ts
    mp = ROOT / "i18n" / "messages.ts"
    mt = mp.read_text(encoding="utf-8")
    added = 0
    for key, zh in DICT.items():
        if f'"{key}"' in mt:
            continue
        # insert before the final "};"
        idx = mt.rfind("\n};")
        if idx == -1:
            print("!! messages.ts closing brace not found")
            return
        esc_key = key.replace('"', '\\"')
        mt = mt[:idx] + f'\n  "{esc_key}": "{zh}",' + mt[idx:]
        added += 1
    if added:
        mp.write_text(mt, encoding="utf-8")
        print(f"✓ messages.ts +{added} keys")
    print("done:", changed)


if __name__ == "__main__":
    main()
