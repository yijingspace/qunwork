"""S12 临时文件治理 (蜂群审计报告 G4) — 工作区临时产物自动清理.

蜂群执行者在任务中会生成大量中间文件: 分块草稿 (_part0X_x.md / _chunk.py /
_fresh_test.txt), 探测文件, 临时脚本 — 任务结束后堆积在工作区, 污染目录
(S12 短板: 临时文件治理, 归档/清理自动化, 工作区整洁)。

本模块在蜂群 run 结束后清理这些遗留物, 但**严格保护正式资产**:
  * `_swarm_reports/` (正式报告 + 备份) — 永不删;
  * `selfmade_tools/` (自造工具资产, 自进化复用) — 永不删;
  * `.qunwork/` (记忆/经验/harness/编排库) — 永不删;
  * 明确的输出文件 (任务 intent 指定的目标文件) — 保留;
  * coordination-report-*.md / hive-health-report-*.md — 保留 (正式报告)。

识别规则 (仅清理"明显是中间产物"的文件):
  * 文件名匹配临时模式: _part* / _chunk* / _fresh* / _test* / _probe* /
    _tmp* / *_temp* / *.tmp / *.pyc / __pycache__;
  * 或: 上次修改时间在本次 run 期间、且非上述受保护目录内的非报告文件。
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 受保护目录 (绝对路径前缀) — 永不清理。
_PROTECTED_DIRS = ("_swarm_reports", "selfmade_tools", ".qunwork", ".coworker", ".git")

# 明确保留的文件名模式 (正式报告)。
_KEEP_FILES = (
    "coordination-report-",
    "hive-health-report-",
    "roi-report-",
)

# 临时/中间产物文件名模式 — 命中即清理。
_TEMP_PATTERNS = (
    re.compile(r"^_?(part|chunk|fresh|probe|tmp|temp|scratch|test)[0-9_]*.*\.(md|txt|py|json|log)$", re.I),
    re.compile(r"\.(tmp|temp|pyc|pyo)$", re.I),
    re.compile(r"^__pycache__$"),
    re.compile(r"^_?draft[0-9_]*\.(md|txt)$", re.I),
)


def _is_protected(path: Path, workspace: Path) -> bool:
    """路径是否在受保护目录内。"""
    try:
        rel = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return True  # 工作区外, 不动
    parts = rel.parts
    return bool(parts and parts[0] in _PROTECTED_DIRS)


def _is_keep_file(name: str) -> bool:
    return any(name.startswith(k) for k in _KEEP_FILES)


def _is_temp_name(name: str) -> bool:
    # search (非 match): `data.tmp` 也要命中后缀正则
    return any(p.search(name) for p in _TEMP_PATTERNS)


def collect_temp_files(
    workspace: str | Path,
    *,
    since: Optional[float] = None,
    keep: Optional[list[str | Path]] = None,
) -> list[Path]:
    """扫描工作区, 返回应清理的临时文件列表 (dry-run 用)。

    since: 只考虑该时间戳之后修改的文件 (run 开始时间); None = 全部。
    keep: 显式豁免的路径 (如本次 run 的正式输出报告) — 名字再像临时也不删。
    """
    ws = Path(workspace)
    keep_set = set()
    for k in keep or []:
        try:
            keep_set.add(Path(k).resolve())
        except (OSError, TypeError):
            pass
    if not ws.is_dir():
        return []
    out: list[Path] = []
    for path in ws.rglob("*"):
        if not path.is_file():
            continue
        if _is_protected(path, ws):
            continue
        if _is_keep_file(path.name):
            continue
        try:
            if path.resolve() in keep_set:
                continue  # 显式豁免 (正式输出)
        except OSError:
            pass
        if since is not None:
            try:
                if path.stat().st_mtime < since:
                    continue  # 旧文件 (run 前就有) — 不是本次遗留
            except OSError:
                continue
        if _is_temp_name(path.name):
            out.append(path)
    return out


def cleanup_workspace_temp_files(
    workspace: str | Path,
    *,
    since: Optional[float] = None,
    keep: Optional[list[str | Path]] = None,
    dry_run: bool = False,
) -> dict:
    """清理工作区临时文件 (S12)。返回 {removed: [paths], protected: n, dry_run}。

    since: 只清理该时间戳之后修改的临时文件 (蜂群 run 期间产生);
           None = 清理所有匹配的临时文件。
    keep: 显式豁免路径 (正式输出报告) — 名字再像临时也不删。
    """
    targets = collect_temp_files(workspace, since=since, keep=keep)
    removed: list[str] = []
    for path in targets:
        if not dry_run:
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("temp cleanup failed %s: %s", path, exc)
                continue
        removed.append(str(path))
    return {"removed": removed, "count": len(removed), "dry_run": dry_run}


def run_started_at() -> float:
    """当前时间戳 — 用于标记本次 run 开始 (since 参数)。"""
    return time.time()
