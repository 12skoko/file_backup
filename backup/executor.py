"""执行器 — 操作计划 → rclone 命令生成 + 按序执行。"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Optional

from .config import Config
from .differ import Plan, MoveOp, UploadOp, TrashOp, MkdirOp
from .reporter import SyncResult


# ═══════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════

def run_plan(plan: Plan, config: Config) -> SyncResult:
    """按序执行操作计划，返回执行结果。"""
    result = SyncResult()

    rclone_bin = config.rclone.binary
    remote = config.target.rclone_remote
    retries = config.rclone.retries
    transfers = config.rclone.transfers
    dry_run = config.sync.dry_run

    # 回收站时间戳
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())

    # ── 1. Trash ──
    for op in plan.trashes:
        src_rel = _rclone_path(op.path, op.pair_index, config)
        trash_rel = _trash_rclone_path(op.path, ts, config)
        cmd = [
            rclone_bin, "moveto",
            f"{remote}:{src_rel}",
            f"{remote}:{trash_rel}",
        ]
        ok, err = _run(cmd, retries, dry_run)
        if ok:
            result.trashed.append({
                "path": op.path,
                "trashed_to": os.path.join(config.trash.dir, ts, op.path).replace("\\", "/"),
                "status": "ok",
            })
        else:
            result.errors.append({"op": "trash", "path": op.path, "error": err})

    # ── 2. Move ──
    for op in plan.moves:
        old_rel = _rclone_path(op.old_path, op.pair_index, config)
        new_rel = _rclone_path(op.new_path, op.pair_index, config)
        cmd = [
            rclone_bin, "moveto",
            f"{remote}:{old_rel}",
            f"{remote}:{new_rel}",
        ]
        ok, err = _run(cmd, retries, dry_run)
        if ok:
            result.moved.append({
                "old": op.old_path,
                "new": op.new_path,
                "status": "ok",
            })
        else:
            result.errors.append({
                "op": "move",
                "old": op.old_path,
                "new": op.new_path,
                "error": err,
            })

    # ── 3. Upload ──
    for op in plan.uploads:
        local_abs = os.path.join(op.local_root, op.path)
        remote_rel = _rclone_path(op.path, op.pair_index, config)

        cmd = [
            rclone_bin, "copy",
            local_abs,
            f"{remote}:{remote_rel}",
            "--retries", str(retries),
            "--transfers", str(transfers),
            "--progress",
        ]
        ok, err = _run(cmd, retries, dry_run)
        if ok:
            result.uploaded.append({
                "path": op.path,
                "size": op.size,
                "status": "ok",
            })
        else:
            result.errors.append({"op": "upload", "path": op.path, "error": err})

    # ── 4. Mkdir ──
    for op in plan.mkdirs:
        remote_rel = _rclone_path(op.path, op.pair_index, config)
        cmd = [
            rclone_bin, "mkdir",
            f"{remote}:{remote_rel}",
        ]
        ok, err = _run(cmd, retries, dry_run)
        if ok:
            result.mkdirs.append({"path": op.path, "status": "ok"})
        else:
            result.errors.append({"op": "mkdir", "path": op.path, "error": err})

    return result


# ═══════════════════════════════════════════════════════════════════
# 路径计算（纯字符串操作，不依赖 os.path.relpath）
# ═══════════════════════════════════════════════════════════════════

def _strip_prefix(path: str, prefix: str) -> str:
    """去掉 path 的 prefix 前缀部分，返回相对路径。

    纯字符串操作，跨平台安全。
    例: _strip_prefix('/backup/photos', '/backup') → 'photos'
        _strip_prefix('/backup', '/backup') → ''
    """
    # 规范化：统一正斜杠，去掉尾斜杠
    p = path.replace("\\", "/").rstrip("/")
    pre = prefix.replace("\\", "/").rstrip("/")

    if p == pre:
        return ""
    if p.startswith(pre + "/"):
        return p[len(pre) + 1:]
    # 前缀不匹配 —— 回退到最后一层目录名
    return p.rsplit("/", 1)[-1] if "/" in p else p


def _rclone_path(rel_path: str, pair_index: int, config: Config) -> str:
    """计算文件在 rclone remote 中的路径。

    rclone 路径 = target 相对 webdav.root 的路径 / rel_path
    例: webdav.root=/backup, target=/backup/photos, rel=2024/me.jpg
         → photos/2024/me.jpg
    """
    target_path = config.paths[pair_index].target
    target_rel = _strip_prefix(target_path, config.webdav.root)
    rel_path = rel_path.replace("\\", "/")

    if not target_rel:
        return rel_path
    return f"{target_rel}/{rel_path}"


def _trash_rclone_path(rel_path: str, timestamp: str, config: Config) -> str:
    """计算回收站中文件的 rclone 路径。"""
    trash_dir = config.trash.dir
    trash_rel = _strip_prefix(trash_dir, config.webdav.root)
    rel_path = rel_path.replace("\\", "/")

    if not trash_rel:
        return f"{timestamp}/{rel_path}"
    return f"{trash_rel}/{timestamp}/{rel_path}"


# ═══════════════════════════════════════════════════════════════════
# 命令执行
# ═══════════════════════════════════════════════════════════════════

def _run(
    cmd: list[str],
    retries: int,
    dry_run: bool,
    timeout: int = 300,
) -> tuple[bool, Optional[str]]:
    """执行命令，返回 (成功?, 错误信息)。"""
    if dry_run:
        print(f"  [DRY RUN] {' '.join(cmd)}")
        return True, None

    last_err = None
    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode == 0:
                return True, None
            last_err = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
            if attempt < retries:
                print(f"  [RETRY {attempt}/{retries}] {' '.join(cmd[:3])}...: {last_err}")
                time.sleep(2)
        except subprocess.TimeoutExpired:
            last_err = f"超时 ({timeout}s)"
            if attempt < retries:
                print(f"  [RETRY {attempt}/{retries}] 超时")
                time.sleep(2)
        except FileNotFoundError:
            return False, f"找不到 rclone 二进制文件: {cmd[0]}"
        except Exception as exc:
            last_err = str(exc)
            if attempt < retries:
                print(f"  [RETRY {attempt}/{retries}] {exc}")
                time.sleep(2)

    return False, last_err
