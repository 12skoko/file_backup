"""文件扫描 — 遍历目录、排除规则、SQLite 缓存、SHA256 哈希。"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config, PathPair


# ═══════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════

@dataclass
class FileInfo:
    """单个文件的元信息。"""
    size: int
    mtime: float
    hash: str


@dataclass
class PairScan:
    """一对路径的扫描结果。"""
    source: str       # A 端源路径（或 B 端本地扫描路径）
    target: str       # B 端目标路径
    files: dict[str, FileInfo]  # rel_path → FileInfo
    dirs: list[str]   # 所有目录（含空目录），按字母序

    def to_dict(self) -> dict:
        """转为 JSON 可序列化的字典。"""
        return {
            "source": self.source,
            "target": self.target,
            "files": {
                path: {"size": fi.size, "mtime": fi.mtime, "hash": fi.hash}
                for path, fi in self.files.items()
            },
            "dirs": sorted(self.dirs),
        }

    @staticmethod
    def from_dict(d: dict) -> "PairScan":
        """从字典还原。"""
        files = {}
        for path, info in d.get("files", {}).items():
            files[path] = FileInfo(
                size=info["size"],
                mtime=info["mtime"],
                hash=info["hash"],
            )
        return PairScan(
            source=d.get("source", ""),
            target=d.get("target", ""),
            files=files,
            dirs=d.get("dirs", []),
        )


# ═══════════════════════════════════════════════════════════════════
# 排除规则
# ═══════════════════════════════════════════════════════════════════

# 内置默认排除（即使没有 .backupignore 也生效）
BUILTIN_EXCLUDES = [
    ".git/",
    ".DS_Store",
    "Thumbs.db",
    "~$*",
]


def load_exclude_patterns(exclude_file: Optional[str] = None) -> list[str]:
    """加载排除规则：内置默认 + .backupignore 文件。"""
    patterns = list(BUILTIN_EXCLUDES)

    if exclude_file:
        path = Path(exclude_file)
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            patterns.append(line)
            except OSError:
                pass  # 读取失败忽略，用内置规则

    return patterns


def should_exclude(rel_path: str, patterns: list[str]) -> bool:
    """检查相对路径是否命中任一排除规则。

    rel_path 统一使用正斜杠。
    """
    rel_path = rel_path.replace("\\", "/")
    parts = rel_path.split("/")

    for pat in patterns:
        # 完整路径匹配
        if fnmatch.fnmatch(rel_path, pat):
            return True
        # 路径组件匹配（如 .git 匹配任意深度的 .git/ 目录）
        for part in parts:
            if fnmatch.fnmatch(part, pat):
                return True
        # 目录前缀匹配：pattern 以 / 结尾，匹配路径前缀
        if pat.endswith("/"):
            prefix = pat  # e.g. "node_modules/"
            if rel_path.startswith(prefix) or ("/" + rel_path).startswith("/" + prefix):
                return True

    return False


# ═══════════════════════════════════════════════════════════════════
# SQLite 缓存
# ═══════════════════════════════════════════════════════════════════

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS file_cache (
    source_root TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    hash        TEXT NOT NULL,
    scanned_at  TEXT NOT NULL,
    PRIMARY KEY (source_root, rel_path)
);
"""

CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_file_cache_hash ON file_cache(hash);
"""

# 用于 size+mtime+source_root 快速查找（rename 检测等场景）
CREATE_MTIME_IDX_SQL = """
CREATE INDEX IF NOT EXISTS idx_file_cache_mtime ON file_cache(source_root, size, mtime);
"""


class FileCache:
    """文件哈希缓存（SQLite）。"""

    def __init__(self, cache_dir: str):
        os.makedirs(cache_dir, exist_ok=True)
        db_path = os.path.join(cache_dir, "cache.db")
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(CREATE_TABLE_SQL)
        self._conn.execute(CREATE_INDEX_SQL)
        self._conn.execute(CREATE_MTIME_IDX_SQL)
        self._conn.commit()

    def get(self, source_root: str, rel_path: str) -> Optional[FileInfo]:
        """查询缓存。返回 None 表示未命中或过期。"""
        row = self._conn.execute(
            "SELECT size, mtime, hash FROM file_cache WHERE source_root = ? AND rel_path = ?",
            (source_root, rel_path),
        ).fetchone()
        if row is None:
            return None
        return FileInfo(size=row[0], mtime=row[1], hash=row[2])

    def put(self, source_root: str, rel_path: str, fi: FileInfo) -> None:
        """写入或更新缓存。"""
        self._conn.execute(
            """INSERT OR REPLACE INTO file_cache
               (source_root, rel_path, size, mtime, hash, scanned_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (source_root, rel_path, fi.size, fi.mtime, fi.hash, _now_iso()),
        )

    def remove(self, source_root: str, rel_path: str) -> None:
        """删除一条缓存记录（文件已不存在时调用）。"""
        self._conn.execute(
            "DELETE FROM file_cache WHERE source_root = ? AND rel_path = ?",
            (source_root, rel_path),
        )

    def clean_stale(self, source_root: str, known_paths: set[str]) -> int:
        """清理无效缓存：source_root 下不在 known_paths 中的记录。"""
        rows = self._conn.execute(
            "SELECT rel_path FROM file_cache WHERE source_root = ?",
            (source_root,),
        ).fetchall()
        stale = [row[0] for row in rows if row[0] not in known_paths]
        for p in stale:
            self._conn.execute(
                "DELETE FROM file_cache WHERE source_root = ? AND rel_path = ?",
                (source_root, p),
            )
        return len(stale)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


# ═══════════════════════════════════════════════════════════════════
# 哈希
# ═══════════════════════════════════════════════════════════════════

BLOCK_SIZE = 64 * 1024  # 64KB


def compute_sha256(file_path: str) -> str:
    """分块计算文件 SHA256。"""
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            block = fh.read(BLOCK_SIZE)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# ═══════════════════════════════════════════════════════════════════
# 扫描主逻辑
# ═══════════════════════════════════════════════════════════════════

def scan(config: Config) -> list[PairScan]:
    """扫描所有路径对，返回 PairScan 列表。"""
    patterns = load_exclude_patterns(config.exclude.file)

    # 回收站目录自动加入排除
    trash_dir = config.trash.dir.rstrip("/").rstrip("\\")
    trash_name = os.path.basename(trash_dir)
    if trash_name:
        patterns.append(trash_name + "/")

    cache = FileCache(config.cache.dir)

    results = []
    for pair in config.paths:
        try:
            ps = _scan_one_pair(pair, patterns, cache)
            results.append(ps)
        except Exception as exc:
            print(f"[WARN] 扫描 {pair.source} 失败: {exc}")

    cache.close()
    return results


def _scan_one_pair(
    pair: PathPair,
    patterns: list[str],
    cache: FileCache,
) -> PairScan:
    """扫描一对路径。"""
    source_root = os.path.realpath(pair.source)
    files: dict[str, FileInfo] = {}
    dirs: list[str] = []

    for dirpath, dirnames, filenames in os.walk(source_root):
        # ── 目录排除 ──
        # 原地修改 dirnames 阻止 os.walk 进入排除目录
        kept_dirs = []
        rel_dir = os.path.relpath(dirpath, source_root)
        if rel_dir == ".":
            rel_dir = ""

        for d in dirnames:
            d_rel = os.path.join(rel_dir, d) if rel_dir else d
            if not should_exclude(d_rel, patterns):
                kept_dirs.append(d)
            else:
                pass  # 跳过排除目录
        dirnames[:] = kept_dirs

        # ── 收集目录 ──
        for d in dirnames:
            d_rel = os.path.join(rel_dir, d) if rel_dir else d
            d_rel = d_rel.replace("\\", "/")
            dirs.append(d_rel)

        # ── 处理文件 ──
        for fname in filenames:
            file_abs = os.path.join(dirpath, fname)
            f_rel = os.path.join(rel_dir, fname) if rel_dir else fname
            f_rel = f_rel.replace("\\", "/")

            if should_exclude(f_rel, patterns):
                continue

            try:
                st = os.lstat(file_abs)
            except OSError:
                continue

            # 跳过符号链接
            if stat.S_ISLNK(st.st_mode):
                continue

            fi = _get_file_info(source_root, f_rel, file_abs, st, cache)
            if fi:
                files[f_rel] = fi

    # 注意：os.walk 的 dirnames 只包含有子项的目录
    # 空目录（完全空的叶子目录）会出现在 dirpath 遍历中但没有子项
    # 我们需要从 dirs 列表推导所有目录（含中间目录）
    all_dirs_set: set[str] = set()
    for d in dirs:
        d = d.replace("\\", "/")
        all_dirs_set.add(d)
        # 添加所有父目录
        parts = d.split("/")
        for i in range(1, len(parts)):
            all_dirs_set.add("/".join(parts[:i]))

    # 根目录本身不算
    sorted_dirs = sorted(d for d in all_dirs_set if d)

    # 清理过期缓存
    stale = cache.clean_stale(source_root, set(files.keys()))
    if stale:
        pass  # 静默清理

    cache.commit()

    return PairScan(
        source=pair.source,
        target=pair.target,
        files=files,
        dirs=sorted_dirs,
    )


def _get_file_info(
    source_root: str,
    rel_path: str,
    abs_path: str,
    st: os.stat_result,
    cache: FileCache,
) -> Optional[FileInfo]:
    """获取文件信息，优先使用缓存。"""
    size = st.st_size
    mtime = st.st_mtime

    # 查缓存
    cached = cache.get(source_root, rel_path)
    if cached is not None and cached.size == size and cached.mtime == mtime:
        return cached  # 命中，复用 hash

    # 计算 hash
    try:
        file_hash = compute_sha256(abs_path)
    except OSError:
        return None

    fi = FileInfo(size=size, mtime=mtime, hash=file_hash)
    cache.put(source_root, rel_path, fi)
    return fi


# ═══════════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    """当前 UTC 时间 ISO 字符串。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
