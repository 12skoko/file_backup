from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class FileEntry:
    name: str
    rel_path: str
    size: int
    mtime: float
    hash: str

    @property
    def path_key(self) -> str:
        return f"{self.name}/{self.rel_path}"


@dataclass(frozen=True)
class DirEntry:
    name: str
    rel_path: str

    @property
    def path_key(self) -> str:
        return f"{self.name}/{self.rel_path}".rstrip("/")


@dataclass
class ScanResult:
    files: dict[str, FileEntry] = field(default_factory=dict)
    dirs: list[DirEntry] = field(default_factory=list)
    target_roots: dict[str, str] = field(default_factory=dict)
    trash_root: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": {key: asdict(value) for key, value in self.files.items()},
            "dirs": [asdict(value) for value in self.dirs],
            "target_roots": self.target_roots,
            "trash_root": self.trash_root,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScanResult":
        files: dict[str, FileEntry] = {}
        for value in data.get("files", {}).values():
            entry = FileEntry(
                name=str(value["name"]),
                rel_path=str(value["rel_path"]),
                size=int(value["size"]),
                mtime=float(value["mtime"]),
                hash=str(value["hash"]),
            )
            files[entry.path_key] = entry
        dirs = [
            DirEntry(name=str(value["name"]), rel_path=str(value["rel_path"]))
            for value in data.get("dirs", [])
        ]
        target_roots = {str(key): str(value).strip("/") for key, value in data.get("target_roots", {}).items()}
        trash_root = data.get("trash_root")
        return cls(files=files, dirs=dirs, target_roots=target_roots, trash_root=str(trash_root).strip("/") if trash_root else None)


@dataclass(frozen=True)
class MoveOp:
    old_path: str
    new_path: str
    confidence: float = 1.0


@dataclass(frozen=True)
class UploadOp:
    path: str
    size: int


@dataclass(frozen=True)
class TrashOp:
    path: str
    size: int = 0


@dataclass(frozen=True)
class MkdirOp:
    path: str


@dataclass
class Plan:
    uploads: list[UploadOp] = field(default_factory=list)
    moves: list[MoveOp] = field(default_factory=list)
    trashes: list[TrashOp] = field(default_factory=list)
    mkdirs: list[MkdirOp] = field(default_factory=list)
