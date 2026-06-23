from __future__ import annotations

from pathlib import Path
from typing import Iterable, Protocol
import hashlib
import os
import sqlite3
from datetime import datetime, timezone

from .config import normalize_rel_path
from .ignore import IgnoreRules
from .models import DirEntry, FileEntry, ScanResult


class ScanPath(Protocol):
    name: str


class RootedScanPath(ScanPath, Protocol):
    source: Path


class TargetScanPath(ScanPath, Protocol):
    target: Path


def scan_paths(paths: Iterable[RootedScanPath | TargetScanPath], cache_dir: Path, ignore: IgnoreRules) -> ScanResult:
    cache = HashCache(cache_dir / "cache.db")
    result = ScanResult()
    try:
        for mapping in paths:
            root = getattr(mapping, "source", None) or getattr(mapping, "target")
            _scan_one(mapping.name, Path(root), cache, ignore, result)
        return result
    finally:
        cache.close()


def hash_file(path: Path, chunk_size: int = 64 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _scan_one(name: str, root: Path, cache: "HashCache", ignore: IgnoreRules, result: ScanResult) -> None:
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        rel_dir = "" if current == root else normalize_rel_path(current.relative_to(root))
        if rel_dir:
            result.dirs.append(DirEntry(name=name, rel_path=rel_dir))

        kept_dirs = []
        for dirname in dirnames:
            full = current / dirname
            if full.is_symlink():
                continue
            child_rel = normalize_rel_path(full.relative_to(root))
            if not ignore.matches(child_rel, is_dir=True):
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            full = current / filename
            if full.is_symlink():
                continue
            rel_path = normalize_rel_path(full.relative_to(root))
            if ignore.matches(rel_path, is_dir=False):
                continue
            try:
                stat = full.stat()
            except OSError:
                continue
            try:
                file_hash = cache.get_or_hash(name, full, stat.st_size, stat.st_mtime)
            except OSError:
                continue
            entry = FileEntry(name=name, rel_path=rel_path, size=stat.st_size, mtime=stat.st_mtime, hash=file_hash)
            result.files[entry.path_key] = entry


class HashCache:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self._init_db()

    def get_or_hash(self, map_name: str, path: Path, size: int, mtime: float) -> str:
        normalized = str(path.resolve())
        row = self.conn.execute(
            "SELECT hash FROM file_cache WHERE map_name=? AND path=? AND size=? AND mtime=?",
            (map_name, normalized, size, mtime),
        ).fetchone()
        if row:
            return str(row[0])
        file_hash = hash_file(path)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO file_cache (map_name, path, size, mtime, hash, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (map_name, normalized, size, mtime, file_hash, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return file_hash

    def _init_db(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_cache (
                map_name   TEXT NOT NULL,
                path       TEXT NOT NULL,
                size       INTEGER NOT NULL,
                mtime      REAL NOT NULL,
                hash       TEXT NOT NULL,
                scanned_at TEXT NOT NULL,
                PRIMARY KEY (map_name, path)
            )
            """
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_file_cache_hash ON file_cache(hash)")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
