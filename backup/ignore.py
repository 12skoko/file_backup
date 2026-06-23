from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import fnmatch


DEFAULT_PATTERNS = [".git/", ".DS_Store", "Thumbs.db", "~$*"]


@dataclass(frozen=True)
class IgnoreRules:
    patterns: list[str]

    def matches(self, rel_path: str, *, is_dir: bool = False) -> bool:
        rel = rel_path.replace("\\", "/").strip("/")
        name = PurePosixPath(rel).name if rel else ""
        for pattern in self.patterns:
            dir_only = pattern.endswith("/")
            normalized = pattern.rstrip("/").strip()
            if not normalized:
                continue
            if dir_only and not is_dir:
                continue
            if fnmatch.fnmatch(name, normalized) or fnmatch.fnmatch(rel, normalized):
                return True
            if dir_only and (rel == normalized or rel.startswith(normalized + "/")):
                return True
        return False


def load_ignore_rules(path) -> IgnoreRules:
    patterns = list(DEFAULT_PATTERNS)
    if path and path.exists():
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if line:
                    patterns.append(line)
    patterns.extend([".backup_trash/", ".backup_cache/"])
    return IgnoreRules(patterns)
