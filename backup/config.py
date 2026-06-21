"""配置管理 — source / target 两套 YAML 格式，统一内部表示。"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


# ═══════════════════════════════════════════════════════════════════
# 配置数据类（内部统一表示）
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
    port: int = 9528


@dataclass
class TargetConfig:
    url: str = ""
    token: str = ""
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
    """完整配置（内部使用）。"""
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

    @property
    def is_source(self) -> bool:
        return self.role == "source"

    @property
    def is_target(self) -> bool:
        return self.role == "target"


# ═══════════════════════════════════════════════════════════════════
# 加载 A 端（source）配置
# ═══════════════════════════════════════════════════════════════════

def load_source_config(config_path: str) -> Config:
    """加载 A 端 (source) 配置文件。

    YAML 格式::

        target:
          url: http://192.168.1.100:9527
          token: "shared-secret"
          rclone_remote: B_webdav

        paths:
          - source: /mnt/photos
            target: /backup/photos

        exclude_file: .backupignore
        cache_dir: /data/.backup

        rclone:
          binary: rclone
          retries: 3
          transfers: 4

        sync:
          dry_run: false
          report_dir: /data/.backup
    """
    raw = _read_yaml(config_path)

    # ── target 连接 ──
    t = raw.get("target", {})
    if not isinstance(t, dict):
        die("target 段必须是字典")

    target = TargetConfig(
        url=_require_str(t, "url", label="target.url"),
        token=_require_str(t, "token", label="target.token"),
        rclone_remote=_get_str(t, "rclone_remote", "B_webdav"),
    )

    # ── 路径 ──
    paths = _parse_source_paths(raw)

    # ── 其他 ──
    exclude = ExcludeConfig(
        file=_get_str(raw, "exclude_file", ".backupignore"),
    )

    cache = CacheConfig(
        dir=_require_str(raw, "cache_dir", label="cache_dir"),
    )

    rclone = _parse_rclone(raw)
    sync = _parse_sync(raw)

    # ========== 修复核心：让 A 端也能读取 webdav 和 trash 配置 ==========
    wd = raw.get("webdav", {})
    webdav = WebdavConfig(
        root=_get_str(wd, "root", "/backup"),
        port=_get_int(wd, "port", 9528),
    )
    trash = _parse_trash(raw)
    # =============================================================

    cfg = Config(
        role="source",
        server=ServerConfig(token=target.token),
        paths=paths,
        exclude=exclude,
        cache=cache,
        trash=trash,          # source 端不使用回收站
        webdav=webdav,        # source 端不启动 WebDAV
        target=target,
        rclone=rclone,
        sync=sync,
    )

    _validate_source(cfg)
    return cfg


# ═══════════════════════════════════════════════════════════════════
# 加载 B 端（target）配置
# ═══════════════════════════════════════════════════════════════════

def load_target_config(config_path: str) -> Config:
    """加载 B 端 (target) 配置文件。

    YAML 格式::

        server:
          port: 9527
          token: "shared-secret"

        webdav:
          port: 9528
          root: /backup

        paths:
          - /backup/photos
          - /backup/docs

        exclude_file: .backupignore
        cache_dir: /data/.backup

        trash:
          dir: /backup/.backup_trash
          keep_days: 30

        rclone:
          binary: rclone
    """
    raw = _read_yaml(config_path)

    # ── 服务 ──
    srv = raw.get("server", {})
    if not isinstance(srv, dict):
        die("server 段必须是字典")

    server = ServerConfig(
        port=_get_int(srv, "port", 9527),
        token=_require_str(srv, "token", label="server.token"),
    )

    # ── WebDAV ──
    wd = raw.get("webdav", {})
    if not isinstance(wd, dict):
        die("webdav 段必须是字典")

    webdav = WebdavConfig(
        root=_require_str(wd, "root", label="webdav.root"),
        port=_get_int(wd, "port", server.port + 1),
    )

    # ── 路径 ──
    paths = _parse_target_paths(raw)

    # ── 其他 ──
    exclude = ExcludeConfig(
        file=_get_str(raw, "exclude_file", ".backupignore"),
    )

    cache = CacheConfig(
        dir=_get_str(raw, "cache_dir", "/data/.backup"),
    )

    trash = _parse_trash(raw)
    rclone = _parse_rclone(raw)

    cfg = Config(
        role="target",
        server=server,
        paths=paths,
        exclude=exclude,
        cache=cache,
        trash=trash,
        webdav=webdav,
        target=TargetConfig(),        # target 端不需要连接对端
        rclone=rclone,
        sync=SyncConfig(),            # target 端不输出报告
    )

    _validate_target(cfg)
    return cfg


# ═══════════════════════════════════════════════════════════════════
# 校验
# ═══════════════════════════════════════════════════════════════════

def _validate_source(cfg: Config) -> None:
    """校验 source 配置。"""
    if not cfg.paths:
        die("paths 至少需要一对 source/target")

    for i, pp in enumerate(cfg.paths):
        if not os.path.isdir(pp.source):
            die(f"paths[{i}].source 路径不存在或不是目录: {pp.source}")

    _warn_overlapping(cfg.paths)

    if not cfg.target.token:
        die("target.token 不能为空")

    if not cfg.target.url:
        die("target.url 不能为空")


def _validate_target(cfg: Config) -> None:
    """校验 target 配置。"""
    if not cfg.server.token:
        die("server.token 不能为空")

    if not cfg.paths:
        die("paths 至少需要一个路径")

    for i, pp in enumerate(cfg.paths):
        if not os.path.isdir(pp.source):
            die(f"paths[{i}] 路径不存在或不是目录: {pp.source}")

        # 所有路径必须在 webdav.root 下
        src = os.path.realpath(pp.source)
        root = os.path.realpath(cfg.webdav.root)
        if not _is_under(src, root):
            die(
                f"paths[{i}] ({pp.source}) 不在 webdav.root "
                f"({cfg.webdav.root}) 下。"
            )

    # 回收站也应在 webdav.root 下
    trash_real = os.path.realpath(cfg.trash.dir)
    root_real = os.path.realpath(cfg.webdav.root)
    if not _is_under(trash_real, root_real):
        warn(
            f"trash.dir ({cfg.trash.dir}) 不在 webdav.root "
            f"({cfg.webdav.root}) 下，rclone 将无法操作回收站。"
        )


# ═══════════════════════════════════════════════════════════════════
# 段落解析
# ═══════════════════════════════════════════════════════════════════

def _parse_source_paths(raw: dict) -> list[PathPair]:
    """解析 source 端的 paths（必须含 source → target 映射）。"""
    raw_paths = raw.get("paths", [])
    if not raw_paths:
        die("paths 至少需要一对 source/target")

    result = []
    for i, entry in enumerate(raw_paths):
        if not isinstance(entry, dict):
            die(f"paths[{i}] 必须是字典，格式: {{source: ..., target: ...}}")
        source = entry.get("source", "")
        target = entry.get("target", "")
        if not source:
            die(f"paths[{i}].source 不能为空")
        if not target:
            die(f"paths[{i}].target 不能为空")
        result.append(PathPair(source=str(source), target=str(target)))
    return result


def _parse_target_paths(raw: dict) -> list[PathPair]:
    """解析 target 端的 paths（字符串列表或字典列表）。"""
    raw_paths = raw.get("paths", [])
    if not raw_paths:
        die("paths 至少需要一个路径")

    result = []
    for i, entry in enumerate(raw_paths):
        if isinstance(entry, str):
            result.append(PathPair(source=entry, target=entry))
        elif isinstance(entry, dict):
            source = entry.get("source", entry.get("path", ""))
            target = entry.get("target", source)
            if not source:
                die(f"paths[{i}] 路径不能为空")
            result.append(PathPair(source=str(source), target=str(target)))
        else:
            die(f"paths[{i}] 必须是字符串或字典")
    return result


def _parse_rclone(raw: dict) -> RcloneConfig:
    """解析 rclone 段。"""
    r = raw.get("rclone", {})
    if not isinstance(r, dict):
        return RcloneConfig()
    return RcloneConfig(
        binary=_get_str(r, "binary", "rclone"),
        retries=_get_int(r, "retries", 3),
        transfers=_get_int(r, "transfers", 4),
    )


def _parse_sync(raw: dict) -> SyncConfig:
    """解析 sync 段。"""
    s = raw.get("sync", {})
    if not isinstance(s, dict):
        return SyncConfig()
    return SyncConfig(
        dry_run=_get_bool(s, "dry_run", False),
        report_dir=_get_str(s, "report_dir", "/data/.backup"),
    )


def _parse_trash(raw: dict) -> TrashConfig:
    """解析 trash 段。"""
    t = raw.get("trash", {})
    if not isinstance(t, dict):
        return TrashConfig()
    return TrashConfig(
        dir=_get_str(t, "dir", "/backup/.backup_trash"),
        keep_days=_get_int(t, "keep_days", 30),
    )


# ═══════════════════════════════════════════════════════════════════
# YAML 读取辅助
# ═══════════════════════════════════════════════════════════════════

def _read_yaml(path_str: str) -> dict:
    """读取 YAML 文件，返回顶层字典。"""
    p = Path(path_str)
    if not p.exists():
        die(f"配置文件不存在: {path_str}")
    with open(p, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        die("配置文件格式错误：顶层必须是字典")
    return raw


def _require_str(d: dict, key: str, label: str = "") -> str:
    """读取必需的非空字符串，缺失则报错退出。"""
    val = d.get(key)
    label = label or key
    if val is None or (isinstance(val, str) and not val.strip()):
        die(f"{label} 必须是非空字符串")
    return str(val)


def _get_str(d: dict, key: str, default: str = "") -> str:
    """读取可选字符串。"""
    val = d.get(key)
    if val is None:
        return default
    return str(val)


def _get_int(d: dict, key: str, default: int = 0) -> int:
    """读取可选整数。"""
    val = d.get(key)
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        die(f"{key} 必须是整数，当前: {val}")
        return default


def _get_bool(d: dict, key: str, default: bool = False) -> bool:
    """读取可选布尔值。"""
    val = d.get(key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    die(f"{key} 必须是布尔值，当前: {val}")
    return default


# ═══════════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════════

def _is_under(child: str, parent: str) -> bool:
    """判断 child 路径是否在 parent 路径之下（含相等）。"""
    try:
        c = os.path.realpath(child).rstrip(os.sep) + os.sep
        p = os.path.realpath(parent).rstrip(os.sep) + os.sep
        return c.startswith(p)
    except (OSError, ValueError):
        return False


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


def die(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)
    sys.exit(1)


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)
