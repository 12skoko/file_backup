from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
import os
import warnings

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised only when dependency is missing
    yaml = None


@dataclass(frozen=True)
class SourcePath:
    name: str
    source: Path


@dataclass(frozen=True)
class TargetPath:
    name: str
    target: Path


@dataclass(frozen=True)
class SourceConfig:
    path: Path
    token: str
    paths: list[SourcePath]
    exclude_file: Path
    cache_dir: Path
    api_url: str
    rclone_remote: str
    scan_poll_interval_sec: float
    scan_timeout_sec: float
    rclone_binary: str
    rclone_retries: int
    rclone_transfers: int
    dry_run: bool
    report_dir: Path


@dataclass(frozen=True)
class TargetConfig:
    path: Path
    host: str
    port: int
    token: str
    webdav_host: str
    webdav_port: int
    webdav_root: Path
    paths: list[TargetPath]
    exclude_file: Path
    cache_dir: Path
    trash_dir: Path
    trash_keep_days: int
    scan_max_workers: int
    job_ttl_sec: int
    result_ttl_sec: int
    graceful_timeout_sec: int
    rclone_binary: str


def load_source_config(config_path: str | Path, *, check_api: bool = False) -> SourceConfig:
    path = _abs_path(config_path)
    data = _read_yaml(path)
    token = _required_str(data, "server.token")
    paths = [
        SourcePath(name=_required_item_str(item, "name"), source=_existing_dir(item, "source"))
        for item in _required_list(data, "paths")
    ]
    _validate_unique_names([p.name for p in paths])
    _warn_overlaps([p.source for p in paths])

    target = _required_dict(data, "target")
    api_url = str(target.get("api_url", "")).rstrip("/")
    if not api_url:
        raise ValueError("target.api_url cannot be empty")
    if check_api:
        _check_api(api_url, token)

    base = path.parent
    cfg = SourceConfig(
        path=path,
        token=token,
        paths=paths,
        exclude_file=_config_path(base, data.get("exclude", {}).get("file", ".backupignore")),
        cache_dir=_ensure_dir(_config_path(base, data.get("cache", {}).get("dir", ".backup"))),
        api_url=api_url,
        rclone_remote=str(target.get("rclone_remote", "")).strip(),
        scan_poll_interval_sec=float(target.get("scan_poll_interval_sec", 2)),
        scan_timeout_sec=float(target.get("scan_timeout_sec", 3600)),
        rclone_binary=str(data.get("rclone", {}).get("binary", "rclone")),
        rclone_retries=int(data.get("rclone", {}).get("retries", 3)),
        rclone_transfers=int(data.get("rclone", {}).get("transfers", 4)),
        dry_run=bool(data.get("sync", {}).get("dry_run", False)),
        report_dir=_ensure_dir(_config_path(base, data.get("sync", {}).get("report_dir", ".backup"))),
    )
    if not cfg.rclone_remote:
        raise ValueError("target.rclone_remote cannot be empty")
    return cfg


def load_target_config(config_path: str | Path) -> TargetConfig:
    path = _abs_path(config_path)
    data = _read_yaml(path)
    token = _required_str(data, "server.token")
    server = _required_dict(data, "server")
    webdav = _required_dict(data, "webdav")
    webdav_root = _existing_dir(webdav, "root")
    paths = [
        TargetPath(name=_required_item_str(item, "name"), target=_existing_dir(item, "target"))
        for item in _required_list(data, "paths")
    ]
    _validate_unique_names([p.name for p in paths])
    _warn_overlaps([p.target for p in paths])

    trash = _required_dict(data, "trash")
    trash_dir = _ensure_dir(_config_path(path.parent, trash.get("dir", "")))
    host = str(server.get("host", "0.0.0.0"))
    port = int(server.get("port", 9527))
    webdav_port = int(webdav.get("port", 9528))
    if port == webdav_port:
        raise ValueError("server.port and webdav.port must be different")
    for target_path in paths:
        _require_inside(target_path.target, webdav_root, f"target path {target_path.target}")
    _require_inside(trash_dir, webdav_root, "trash.dir")

    scan = data.get("scan", {})
    return TargetConfig(
        path=path,
        host=host,
        port=port,
        token=token,
        webdav_host=str(webdav.get("host", "0.0.0.0")),
        webdav_port=webdav_port,
        webdav_root=webdav_root,
        paths=paths,
        exclude_file=_config_path(path.parent, data.get("exclude", {}).get("file", ".backupignore")),
        cache_dir=_ensure_dir(_config_path(path.parent, data.get("cache", {}).get("dir", ".backup_cache"))),
        trash_dir=trash_dir,
        trash_keep_days=int(trash.get("keep_days", 30)),
        scan_max_workers=int(scan.get("max_workers", 1)),
        job_ttl_sec=int(scan.get("job_ttl_sec", 3600)),
        result_ttl_sec=int(scan.get("result_ttl_sec", 3600)),
        graceful_timeout_sec=int(scan.get("graceful_timeout_sec", 10)),
        rclone_binary=str(data.get("rclone", {}).get("binary", "rclone")),
    )


def target_rel_to_webdav(path: Path, webdav_root: Path) -> str:
    rel = path.resolve().relative_to(webdav_root.resolve())
    return rel.as_posix()


def normalize_rel_path(path: str | Path) -> str:
    raw = Path(path).as_posix()
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError(f"relative path cannot contain '..': {path}")
    return "/".join(parts)


def _read_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install dependencies with: python -m pip install -r requirements.txt")
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def _abs_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _config_path(base: Path, value: Any) -> Path:
    p = Path(str(value)).expanduser()
    if not p.is_absolute():
        p = base / p
    return p.resolve()


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _existing_dir(item: dict[str, Any], key: str) -> Path:
    p = _abs_path(_required_item_str(item, key))
    if not p.is_dir():
        raise ValueError(f"{key} must exist and be a directory: {p}")
    return p


def _required_dict(data: dict[str, Any], dotted: str) -> dict[str, Any]:
    value: Any = data
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"{dotted} is required")
        value = value[part]
    if not isinstance(value, dict):
        raise ValueError(f"{dotted} must be a mapping")
    return value


def _required_list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{key} must be a non-empty list")
    if not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{key} items must be mappings")
    return value


def _required_str(data: dict[str, Any], dotted: str) -> str:
    value: Any = data
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"{dotted} is required")
        value = value[part]
    result = str(value).strip()
    if not result:
        raise ValueError(f"{dotted} cannot be empty")
    return result


def _required_item_str(item: dict[str, Any], key: str) -> str:
    result = str(item.get(key, "")).strip()
    if not result:
        raise ValueError(f"{key} cannot be empty")
    return result


def _validate_unique_names(names: list[str]) -> None:
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate path names: {', '.join(duplicates)}")


def _warn_overlaps(paths: list[Path]) -> None:
    normalized = [Path(os.path.normcase(str(p.resolve()))) for p in paths]
    for i, left in enumerate(normalized):
        for j, right in enumerate(normalized):
            if i >= j:
                continue
            if _is_relative_to(left, right) or _is_relative_to(right, left):
                warnings.warn(f"path mappings overlap: {paths[i]} and {paths[j]}", RuntimeWarning)


def _require_inside(child: Path, parent: Path, label: str) -> None:
    if not _is_relative_to(child.resolve(), parent.resolve()):
        raise ValueError(f"{label} must be under webdav.root: {parent}")


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_api(api_url: str, token: str) -> None:
    req = Request(f"{api_url}/health", headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=5) as response:
            if response.status >= 400:
                raise ValueError(f"target.api_url health check failed: HTTP {response.status}")
    except OSError as exc:
        raise ValueError(f"target.api_url is not reachable: {api_url}") from exc
