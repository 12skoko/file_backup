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

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": {key: asdict(value) for key, value in self.files.items()},
            "dirs": [asdict(value) for value in self.dirs],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScanResult":
        files = {
            key: FileEntry(
                name=str(value["name"]),
                rel_path=str(value["rel_path"]),
                size=int(value["size"]),
                mtime=float(value["mtime"]),
                hash=str(value["hash"]),
            )
            for key, value in data.get("files", {}).items()
        }
        dirs = [
            DirEntry(name=str(value["name"]), rel_path=str(value["rel_path"]))
            for value in data.get("dirs", [])
        ]
        return cls(files=files, dirs=dirs)


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
    extra_files: list[TrashOp] = field(default_factory=list)
    extra_dirs: list[str] = field(default_factory=list)
