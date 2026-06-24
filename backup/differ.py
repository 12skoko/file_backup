from __future__ import annotations

from collections import defaultdict

from .models import FileEntry, MkdirOp, MoveOp, Plan, ScanResult, TrashOp, UploadOp


def compare(scan_a: ScanResult, scan_b: ScanResult, *, allow_cross_map_moves: bool = False) -> Plan:
    plan = Plan()
    matched_a: set[str] = set()
    matched_b: set[str] = set()

    for path_key, a_entry in scan_a.files.items():
        b_entry = scan_b.files.get(path_key)
        if b_entry and b_entry.hash == a_entry.hash:
            matched_a.add(path_key)
            matched_b.add(path_key)

    a_by_hash = _by_hash(scan_a.files, excluded=matched_a)
    b_by_hash = _by_hash(scan_b.files, excluded=matched_b)
    for file_hash, a_entries in a_by_hash.items():
        candidates = b_by_hash.get(file_hash, [])
        for a_entry in a_entries:
            best = _best_move_candidate(a_entry, candidates, matched_b, allow_cross_map_moves)
            if not best:
                continue
            matched_a.add(a_entry.path_key)
            matched_b.add(best.path_key)
            candidates.remove(best)
            plan.moves.append(MoveOp(old_path=best.path_key, new_path=a_entry.path_key, confidence=_confidence(a_entry, best)))

    for path_key, a_entry in sorted(scan_a.files.items()):
        if path_key not in matched_a:
            plan.uploads.append(UploadOp(path=path_key, size=a_entry.size))

    for path_key, b_entry in sorted(scan_b.files.items()):
        if path_key not in matched_b:
            plan.trashes.append(TrashOp(path=path_key, size=b_entry.size))

    a_dirs = {d.path_key for d in scan_a.dirs}
    b_dirs = {d.path_key for d in scan_b.dirs}
    for path in sorted(a_dirs - b_dirs):
        plan.mkdirs.append(MkdirOp(path=path))
    for path in sorted(b_dirs - a_dirs):
        if _dir_has_unmatched_files(path, scan_b.files, matched_b):
            plan.extra_dirs.append(path)
        else:
            plan.extra_dirs.append(path)

    return plan


def _by_hash(files: dict[str, FileEntry], excluded: set[str]) -> dict[str, list[FileEntry]]:
    grouped: dict[str, list[FileEntry]] = defaultdict(list)
    for path_key, entry in files.items():
        if path_key not in excluded:
            grouped[entry.hash].append(entry)
    return grouped


def _best_move_candidate(
    a_entry: FileEntry,
    candidates: list[FileEntry],
    matched_b: set[str],
    allow_cross_map_moves: bool,
) -> FileEntry | None:
    usable = [entry for entry in candidates if entry.path_key not in matched_b]
    if not allow_cross_map_moves:
        usable = [entry for entry in usable if entry.name == a_entry.name]
    if not usable:
        return None
    return max(usable, key=lambda b_entry: _confidence(a_entry, b_entry))


def _confidence(a_entry: FileEntry, b_entry: FileEntry) -> float:
    score = 0.55
    if a_entry.name == b_entry.name:
        score += 0.2
    if _basename(a_entry.rel_path) == _basename(b_entry.rel_path):
        score += 0.15
    if _parent(a_entry.rel_path) == _parent(b_entry.rel_path):
        score += 0.1
    return min(score, 1.0)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _dir_has_unmatched_files(dir_key: str, files: dict[str, FileEntry], matched: set[str]) -> bool:
    prefix = dir_key.rstrip("/") + "/"
    return any(path_key.startswith(prefix) and path_key not in matched for path_key in files)
