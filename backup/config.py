"""配置管理 — YAML 读取、校验、类型化访问。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


# ═══════════════════════════════════════════════════════════════════
# 配置数据类
# ═══════════════════════════════════════════════════════════════════

@dataclass
class PathPair:
    """一对路径映射：A 端路径 → B 端路径。"""
    source: str
    target: str


@dataclass
class ServerConfig:
    port: int = 9527
    token: str = ""


@dataclass
class ExcludeConfig:
    file: str = ".backupignore"


@dataclass
class CacheConfig:
    dir: str = "/data/.backup"


@dataclass
class TrashConfig:
    dir: str = "/backup/.backup_trash"
    keep_days: int = 30


@dataclass
class WebdavConfig:
    root: str = "/backup"


@dataclass
class TargetConfig:
    url: str = ""
    rclone_remote: str = "B_webdav"


@dataclass
class RcloneConfig:
    binary: str = "rclone"
    retries: int = 3
    transfers: int = 4


@dataclass
class SyncConfig:
    dry_run: bool = False
    report_dir: str = "/data/.backup"


@dataclass
class Config:
    """完整配置。"""
    role: str                      # "source" | "target"
    server: ServerConfig
    paths: list[PathPair]
    exclude: ExcludeConfig
    cache: CacheConfig
    trash: TrashConfig
    webdav: WebdavConfig
    target: TargetConfig
    rclone: RcloneConfig
    sync: SyncConfig

    # ── 派生属性 ──

    @property
    def is_source(self) -> bool:
        return self.role == "source"

    @property
    def is_target(self) -> bool:
        return self.role == "target"


# ═══════════════════════════════════════════════════════════════════
# 加载 & 校验
# ═══════════════════════════════════════════════════════════════════

def load_config(config_path: str) -> Config:
    """从 YAML 文件加载并校验配置。"""
    path = Path(config_path)
    if not path.exists():
        die(f"配置文件不存在: {config_path}")

    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if not isinstance(raw, dict):
        die("配置文件格式错误：顶层必须是字典")

    # ── 解析各段 ──

    role = _require_str(raw, "role")

    server = ServerConfig(
        port=_get_int(raw, "server", "port", default=9527),
        token=_require_str(raw, "server", "token", label="server.token"),
    )

    paths = _parse_paths(raw)

    exclude = ExcludeConfig(
        file=_get_str(raw, "exclude", "file", default=".backupignore"),
    )

    cache = CacheConfig(
        dir=_require_str(raw, "cache", "dir", label="cache.dir"),
    )

    trash = TrashConfig(
        dir=_require_str(raw, "trash", "dir", label="trash.dir"),
        keep_days=_get_int(raw, "trash", "keep_days", default=30),
    )

    webdav = WebdavConfig(
        root=_require_str(raw, "webdav", "root", label="webdav.root"),
    )

    target = TargetConfig(
        url=_get_str(raw, "target", "url", default=""),
        rclone_remote=_get_str(raw, "target", "rclone_remote", default="B_webdav"),
    )

    rclone = RcloneConfig(
        binary=_get_str(raw, "rclone", "binary", default="rclone"),
        retries=_get_int(raw, "rclone", "retries", default=3),
        transfers=_get_int(raw, "rclone", "transfers", default=4),
    )

    sync = SyncConfig(
        dry_run=_get_bool(raw, "sync", "dry_run", default=False),
        report_dir=_require_str(raw, "sync", "report_dir", label="sync.report_dir"),
    )

    cfg = Config(
        role=role,
        server=server,
        paths=paths,
        exclude=exclude,
        cache=cache,
        trash=trash,
        webdav=webdav,
        target=target,
        rclone=rclone,
        sync=sync,
    )

    # ── 校验 ──

    _validate(cfg)

    return cfg


def _validate(cfg: Config) -> None:
    """校验配置合法性。"""

    # role
    if cfg.role not in ("source", "target"):
        die(f"role 必须是 source 或 target，当前: {cfg.role}")

    # paths
    if not cfg.paths:
        die("paths 至少需要一对 source/target")

    for i, pp in enumerate(cfg.paths):
        if cfg.is_source:
            if not os.path.isdir(pp.source):
                die(f"paths[{i}].source 路径不存在或不是目录: {pp.source}")
        else:
            # target 模式下 source 字段作为本地扫描路径
            if not os.path.isdir(pp.source):
                die(f"paths[{i}].source 路径不存在或不是目录: {pp.source}")

            # target 模式下所有 source 必须在 webdav.root 下
            src = os.path.realpath(pp.source)
            root = os.path.realpath(cfg.webdav.root)
            if not _is_under(src, root):
                die(
                    f"paths[{i}].source ({pp.source}) 不在 webdav.root "
                    f"({cfg.webdav.root}) 下。target 模式下所有扫描路径必须"
                    f"在 WebDAV 根目录内。"
                )

    # 多 path 之间有重叠警告
    _warn_overlapping(cfg.paths)

    # token
    if not cfg.server.token:
        die("server.token 不能为空")

    # source 模式额外检查
    if cfg.is_source:
        if not cfg.target.url:
            die("source 模式下 target.url 不能为空")
        if not cfg.target.rclone_remote:
            die("source 模式下 target.rclone_remote 不能为空")

    # trash 应该在 webdav.root 下（rclone 需要能访问）
    trash_real = os.path.realpath(cfg.trash.dir)
    root_real = os.path.realpath(cfg.webdav.root)
    if cfg.is_target and not _is_under(trash_real, root_real):
        warn(
            f"trash.dir ({cfg.trash.dir}) 不在 webdav.root "
            f"({cfg.webdav.root}) 下，rclone 将无法操作回收站。"
            f"建议将 trash.dir 设置在 webdav.root 内。"
        )


def _warn_overlapping(paths: list[PathPair]) -> None:
    """检查多对路径之间是否有重叠。"""
    resolved = []
    for pp in paths:
        try:
            resolved.append((pp.source, os.path.realpath(pp.source)))
        except OSError:
            continue

    for i in range(len(resolved)):
        for j in range(i + 1, len(resolved)):
            si, ri = resolved[i]
            sj, rj = resolved[j]
            if _is_under(ri, rj) or _is_under(rj, ri):
                warn(f"paths[{i}].source ({si}) 与 paths[{j}].source ({sj}) 路径有重叠")


# ═══════════════════════════════════════════════════════════════════
# YAML 读取辅助
# ═══════════════════════════════════════════════════════════════════

def _get(raw: dict, *keys: str):
    """逐层取值，任意层缺失返回 None。"""
    cur = raw
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
        if cur is None:
            return None
    return cur


def _require_str(raw: dict, *keys: str, label: str = "") -> str:
    """逐层取字符串值，缺失或为空则退出。"""
    val = _get(raw, *keys)
    label = label or '.'.join(keys)
    if val is None or (isinstance(val, str) and not val.strip()):
        die(f"{label} 必须是非空字符串")
    return str(val)


def _get_str(raw: dict, *keys: str, default: str = "") -> str:
    """逐层取字符串值，缺失返回默认。"""
    val = _get(raw, *keys)
    if val is None:
        return default
    return str(val)


def _get_int(raw: dict, *keys: str, default: int = 0) -> int:
    """逐层取整数值。"""
    val = _get(raw, *keys)
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        die(f"{'.'.join(keys)} 必须是整数，当前: {val}")
        return default


def _get_bool(raw: dict, *keys: str, default: bool = False) -> bool:
    """逐层取布尔值。"""
    val = _get(raw, *keys)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    die(f"{'.'.join(keys)} 必须是布尔值，当前: {val}")
    return default


def _parse_paths(raw: dict) -> list[PathPair]:
    """解析 paths 列表。"""
    raw_paths = raw.get("paths", [])
    if not raw_paths:
        die("paths 至少需要一对 source/target")

    result = []
    for i, entry in enumerate(raw_paths):
        if not isinstance(entry, dict):
            die(f"paths[{i}] 必须是字典")
        source = entry.get("source", "")
        target = entry.get("target", "")
        if not source:
            die(f"paths[{i}].source 不能为空")
        if not target:
            die(f"paths[{i}].target 不能为空")
        result.append(PathPair(source=str(source), target=str(target)))
    return result


# ═══════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════

def _is_under(child: str, parent: str) -> bool:
    """判断 child 路径是否在 parent 路径之下（含相等）。"""
    try:
        c = os.path.realpath(child).rstrip(os.sep) + os.sep
        p = os.path.realpath(parent).rstrip(os.sep) + os.sep
        return c.startswith(p)
    except (OSError, ValueError):
        return False


def die(msg: str) -> None:
    """打印错误并退出。"""
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    """打印警告。"""
    print(f"[WARN] {msg}", file=sys.stderr)
