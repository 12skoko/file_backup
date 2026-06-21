"""报告生成 — diff 报告和 sync 报告。"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from .differ import Plan


# ═══════════════════════════════════════════════════════════════════
# 报告数据结构
# ═══════════════════════════════════════════════════════════════════

@dataclass
class DiffReport:
    """差异报告（执行前预览）。"""
    timestamp: str
    summary: dict
    uploads: list[dict] = field(default_factory=list)
    moves: list[dict] = field(default_factory=list)
    trashes: list[dict] = field(default_factory=list)
    mkdirs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "type": "diff",
            "timestamp": self.timestamp,
            "summary": self.summary,
            "uploads": self.uploads,
            "moves": self.moves,
            "trashes": self.trashes,
            "mkdirs": self.mkdirs,
        }


@dataclass
class SyncResult:
    """单次同步的执行结果。"""
    uploaded: list[dict] = field(default_factory=list)
    moved: list[dict] = field(default_factory=list)
    trashed: list[dict] = field(default_factory=list)
    mkdirs: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)


@dataclass
class SyncReport:
    """同步报告（执行后）。"""
    timestamp: str
    duration_sec: float
    diff: dict
    results: dict

    def to_dict(self) -> dict:
        return {
            "type": "sync",
            "timestamp": self.timestamp,
            "duration_sec": self.duration_sec,
            "diff": self.diff,
            "results": self.results,
        }


# ═══════════════════════════════════════════════════════════════════
# 生成 diff 报告
# ═══════════════════════════════════════════════════════════════════

def build_diff_report(plan: Plan) -> DiffReport:
    """从 Plan 生成差异报告。"""
    uploads = [
        {
            "path": op.path,
            "size": op.size,
            "pair_index": op.pair_index,
            "local_root": op.local_root,
            "target_root": op.target_root,
        }
        for op in plan.uploads
    ]
    moves = [
        {"old": op.old_path, "new": op.new_path, "pair_index": op.pair_index}
        for op in plan.moves
    ]
    trashes = [
        {"path": op.path, "size": op.size, "pair_index": op.pair_index, "is_dir": op.is_dir}
        for op in plan.trashes
    ]
    mkdirs = [op.path for op in plan.mkdirs]

    return DiffReport(
        timestamp=_now_iso(),
        summary={
            "will_upload": len(uploads),
            "will_move": len(moves),
            "will_trash": len(trashes),
            "will_mkdir": len(mkdirs),
            "total_bytes_upload": plan.total_upload_bytes,
        },
        uploads=uploads,
        moves=moves,
        trashes=trashes,
        mkdirs=mkdirs,
    )


# ═══════════════════════════════════════════════════════════════════
# 保存 & 格式化
# ═══════════════════════════════════════════════════════════════════

def save_diff_report(report: DiffReport, report_dir: str) -> str:
    """保存 diff 报告到文件，返回文件路径。"""
    os.makedirs(report_dir, exist_ok=True)
    ts = report.timestamp.replace(":", "").replace("-", "").replace("T", "_")
    filename = f"diff_{ts}.json"
    filepath = os.path.join(report_dir, filename)
    _write_json(filepath, report.to_dict())
    return filepath


def save_sync_report(report: SyncReport, report_dir: str) -> str:
    """保存 sync 报告到文件，返回文件路径。"""
    os.makedirs(report_dir, exist_ok=True)
    ts = report.timestamp.replace(":", "").replace("-", "").replace("T", "_")
    filename = f"sync_{ts}.json"
    filepath = os.path.join(report_dir, filename)
    _write_json(filepath, report.to_dict())
    return filepath


def format_diff_text(report: DiffReport) -> str:
    """生成可读的文本差异报告。"""
    s = report.summary
    # 区分文件和目录的回收数量
    trash_files = sum(1 for t in report.trashes if not t.get("is_dir", False))
    trash_dirs = sum(1 for t in report.trashes if t.get("is_dir", False))
    lines = [
        "=" * 60,
        "  差异报告",
        "=" * 60,
        f"时间: {report.timestamp}",
        "",
        "── 摘要 ──",
        f"  新增（上传）:  {s['will_upload']} 个文件  ({_fmt_size(s['total_bytes_upload'])})",
        f"  移动（改名）:  {s['will_move']} 个文件",
        f"  回收文件:      {trash_files} 个",
        f"  回收目录:      {trash_dirs} 个",
        f"  新建目录:      {s['will_mkdir']} 个",
    ]

    if report.uploads:
        lines.append("")
        lines.append("── 新增文件 ──")
        for f in report.uploads[:50]:
            lines.append(f"  + {f['path']}  ({_fmt_size(f['size'])})")
        if len(report.uploads) > 50:
            lines.append(f"  ... 还有 {len(report.uploads) - 50} 个")

    if report.moves:
        lines.append("")
        lines.append("── 移动文件 ──")
        for m in report.moves[:30]:
            lines.append(f"  → {m['old']}  ==>  {m['new']}")
        if len(report.moves) > 30:
            lines.append(f"  ... 还有 {len(report.moves) - 30} 个")

    if report.trashes:
        lines.append("")
        lines.append("── 回收项目 ──")
        for t in report.trashes[:30]:
            tag = "[目录]" if t.get("is_dir") else ""
            lines.append(f"  ✕ {t['path']}  {tag}  ({_fmt_size(t['size'])})")
        if len(report.trashes) > 30:
            lines.append(f"  ... 还有 {len(report.trashes) - 30} 个")

    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)


def format_sync_text(
    diff_report: DiffReport,
    sync_result: SyncResult,
    duration_sec: float,
) -> str:
    """生成可读的文本同步报告。"""
    lines = [
        "=" * 60,
        "  同步报告",
        "=" * 60,
        f"耗时: {duration_sec:.1f} 秒",
        "",
        f"  上传:  {len(sync_result.uploaded)} 成功",
        f"  移动:  {len(sync_result.moved)} 成功",
        f"  回收:  {len(sync_result.trashed)} 成功",
        f"  建目录: {len(sync_result.mkdirs)} 成功",
        f"  错误:  {len(sync_result.errors)}",
    ]

    if sync_result.errors:
        lines.append("")
        lines.append("── 错误 ──")
        for err in sync_result.errors:
            lines.append(f"  !! {err.get('op', '?')}: {err.get('error', '?')}")

    lines.append("")
    lines.append("=" * 60)

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════════

def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fmt_size(size: int) -> str:
    """人类可读的文件大小。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size} {unit}"
        size //= 1024
    return f"{size} PB"
