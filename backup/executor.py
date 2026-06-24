from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
import subprocess

from .config import SourceConfig, TargetConfig, target_rel_to_webdav
from .models import MkdirOp, MoveOp, Plan, TrashOp, UploadOp


def run_plan(
    plan: Plan,
    source: SourceConfig,
    target: TargetConfig | None = None,
    *,
    target_roots: dict[str, str] | None = None,
    trash_root: str | None = None,
) -> dict[str, Any]:
    runner = RcloneRunner(source, target_roots=target_roots or {}, trash_root=trash_root)
    return {
        "mkdirs": [runner.mkdir(op, target) for op in plan.mkdirs],
        "trashed": [runner.trash(op, target) for op in plan.trashes],
        "moved": [runner.move(op, target) for op in plan.moves],
        "uploaded": [runner.upload(op, source, target) for op in plan.uploads],
        "errors": runner.errors,
    }


class RcloneRunner:
    def __init__(self, source: SourceConfig, *, target_roots: dict[str, str] | None = None, trash_root: str | None = None):
        self.source = source
        self.target_roots = target_roots or {}
        self.trash_root = trash_root
        self.errors: list[dict[str, Any]] = []
        self.trash_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    def mkdir(self, op: MkdirOp, target: TargetConfig | None) -> dict[str, Any]:
        remote_path = self._remote_path(op.path, target)
        return self._run("mkdir", op.path, [self.source.rclone_binary, "mkdir", self._remote(remote_path)], {"remote": remote_path})

    def trash(self, op: TrashOp, target: TargetConfig | None) -> dict[str, Any]:
        remote_path = self._remote_path(op.path, target)
        dest = self._trash_path(op.path, target)
        return self._run("trash", op.path, [self.source.rclone_binary, "moveto", self._remote(remote_path), self._remote(dest)], {"remote": remote_path, "trashed_to": dest})

    def move(self, op: MoveOp, target: TargetConfig | None) -> dict[str, Any]:
        old_remote = self._remote_path(op.old_path, target)
        new_remote = self._remote_path(op.new_path, target)
        return self._run("move", op.old_path, [self.source.rclone_binary, "moveto", self._remote(old_remote), self._remote(new_remote)], {"new": op.new_path, "remote": old_remote, "new_remote": new_remote, "confidence": op.confidence})

    def upload(self, op: UploadOp, source: SourceConfig, target: TargetConfig | None) -> dict[str, Any]:
        local = _local_path_for_upload(op.path, source)
        remote = self._remote_path(op.path, target)
        cmd = [
            source.rclone_binary,
            "copyto",
            str(local),
            self._remote(remote),
            "--retries",
            str(source.rclone_retries),
            "--transfers",
            str(source.rclone_transfers),
        ]
        return self._run("upload", op.path, cmd, {"local": str(local)})

    def _run(self, action: str, path: str, cmd: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"path": path, "status": "dry-run" if self.source.dry_run else "ok"}
        if extra:
            payload.update(extra)
        if self.source.dry_run:
            payload["command"] = cmd
            return payload
        try:
            completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
            if completed.stdout.strip():
                payload["stdout"] = completed.stdout.strip()
            return payload
        except subprocess.CalledProcessError as exc:
            error = {
                "action": action,
                "path": path,
                "status": "error",
                "returncode": exc.returncode,
                "stderr": exc.stderr.strip(),
                "command": cmd,
            }
            self.errors.append(error)
            return error

    def _remote(self, path: str) -> str:
        return f"{self.source.rclone_remote}:{path}"

    def _remote_path(self, path: str, target: TargetConfig | None) -> str:
        if target is not None:
            return _target_webdav_path(path, target)
        name, rel_path = _split_key(path)
        target_root = self.target_roots.get(name)
        if target_root is None:
            return path
        return f"{target_root.strip('/')}/{rel_path}".strip("/")

    def _trash_path(self, path: str, target: TargetConfig | None) -> str:
        if target is None:
            if self.trash_root:
                return f"{self.trash_root.strip('/')}/{self.trash_timestamp}/{path}"
            return f".backup_trash/{self.trash_timestamp}/{path}"
        trash_rel = target_rel_to_webdav(target.trash_dir, target.webdav_root)
        return f"{trash_rel}/{self.trash_timestamp}/{path}"


def _local_path_for_upload(path_key: str, source: SourceConfig) -> Path:
    name, rel_path = _split_key(path_key)
    for mapping in source.paths:
        if mapping.name == name:
            return mapping.source / rel_path
    raise KeyError(f"unknown source mapping: {name}")


def _target_webdav_path(path_key: str, target: TargetConfig) -> str:
    name, rel_path = _split_key(path_key)
    for mapping in target.paths:
        if mapping.name == name:
            target_root = target_rel_to_webdav(mapping.target, target.webdav_root)
            return f"{target_root}/{rel_path}".strip("/")
    raise KeyError(f"unknown target mapping: {name}")


def _split_key(path_key: str) -> tuple[str, str]:
    path_key = path_key.strip("/")
    if "/" not in path_key:
        raise ValueError(f"invalid path key: {path_key}")
    name, rel_path = path_key.split("/", 1)
    if not name or not rel_path:
        raise ValueError(f"invalid path key: {path_key}")
    return name, rel_path
