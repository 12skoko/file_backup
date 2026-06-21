"""智能对比 — hash 匹配、移动检测、生成操作计划。"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .scanner import PairScan


# ═══════════════════════════════════════════════════════════════════
# 操作类型
# ═══════════════════════════════════════════════════════════════════

@dataclass
class MoveOp:
    """B 端文件重命名／移动。"""
    old_path: str      # B 端当前路径（相对）
    new_path: str      # B 端目标路径（相对）
    pair_index: int
    size: int = 0


@dataclass
class UploadOp:
    """上传新文件到 B 端。"""
    path: str          # 相对路径
    size: int
    local_root: str    # A 端源目录（绝对路径）
    target_root: str   # B 端目标目录
    pair_index: int


@dataclass
class TrashOp:
    """将 B 端文件／目录移入回收站。"""
    path: str          # 相对路径
    size: int
    pair_index: int
    is_dir: bool = False


@dataclass
class MkdirOp:
    """在 B 端创建目录。"""
    path: str          # 相对路径
    pair_index: int


@dataclass
class Plan:
    """差异操作计划。"""
    uploads: list[UploadOp] = field(default_factory=list)
    moves: list[MoveOp] = field(default_factory=list)
    trashes: list[TrashOp] = field(default_factory=list)
    mkdirs: list[MkdirOp] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not any([
            self.uploads, self.moves, self.trashes, self.mkdirs,
        ])

    @property
    def total_upload_bytes(self) -> int:
        return sum(op.size for op in self.uploads)


# ═══════════════════════════════════════════════════════════════════
# 对比入口
# ═══════════════════════════════════════════════════════════════════

def compare(scan_a: list[PairScan], scan_b: list[PairScan]) -> Plan:
    """对比 A/B 两端的扫描结果，生成操作计划。

    scan_a / scan_b 按 pair_index 对齐。
    """
    if len(scan_a) != len(scan_b):
        raise ValueError(
            f"A 端和 B 端的路径对数量不一致: {len(scan_a)} vs {len(scan_b)}"
        )

    plan = Plan()

    for idx, (pa, pb) in enumerate(zip(scan_a, scan_b)):
        _diff_one_pair(idx, pa, pb, plan)

    return plan


def _diff_one_pair(
    pair_index: int,
    a: PairScan,
    b: PairScan,
    plan: Plan,
) -> None:
    """对比一对路径。"""
    a_files = a.files   # rel_path → FileInfo
    b_files = b.files

    # ── 构建 hash 索引 ──
    # a_hash_map: hash → [rel_path, ...]
    a_hash_map: dict[str, list[str]] = {}
    for path, fi in a_files.items():
        a_hash_map.setdefault(fi.hash, []).append(path)

    # b_hash_map: hash → [rel_path, ...]
    b_hash_map: dict[str, list[str]] = {}
    for path, fi in b_files.items():
        b_hash_map.setdefault(fi.hash, []).append(path)

    # 已处理的 B 端路径
    matched_b: set[str] = set()

    # ── 1. hash 相同、路径相同 → 不变，标记为已匹配 ──
    all_hashes = set(a_hash_map.keys()) & set(b_hash_map.keys())
    for h in all_hashes:
        a_paths = a_hash_map[h]
        b_paths = b_hash_map[h]
        for ap in a_paths:
            if ap in b_paths and ap not in matched_b:
                matched_b.add(ap)

    # ── 2. hash 相同、路径不同 → Move ──
    for h in all_hashes:
        a_paths = [p for p in a_hash_map[h]]
        b_paths = [p for p in b_hash_map[h] if p not in matched_b]

        # 一对一匹配：按路径相似度
        pairs = _match_by_similarity(a_paths, b_paths)
        for ap, bp in pairs:
            plan.moves.append(MoveOp(
                old_path=bp,
                new_path=ap,
                pair_index=pair_index,
                size=a_files[ap].size,
            ))
            matched_b.add(bp)

    # ── 3. A 有 B 无 → Upload ──
    matched_a: set[str] = set()
    for move in plan.moves:
        if move.pair_index == pair_index:
            matched_a.add(move.new_path)
    # 同路径同 hash 的也算已匹配
    for h in all_hashes:
        for ap in a_hash_map[h]:
            if ap in b_files:
                matched_a.add(ap)

    for path, fi in a_files.items():
        if path not in matched_a:
            plan.uploads.append(UploadOp(
                path=path,
                size=fi.size,
                local_root=a.source,
                target_root=a.target,
                pair_index=pair_index,
            ))

    # ── 4. B 有 A 无 → Trash（直接移入回收站）──
    for path, fi in b_files.items():
        if path not in matched_b:
            plan.trashes.append(TrashOp(
                path=path,
                size=fi.size,
                pair_index=pair_index,
            ))

    # ── 目录 diff ──
    a_dirs = set(a.dirs)
    b_dirs = set(b.dirs)

    # A 有 B 无 → Mkdir
    for d in sorted(a_dirs - b_dirs):
        plan.mkdirs.append(MkdirOp(path=d, pair_index=pair_index))

    # B 有 A 无 → Trash（目录也移入回收站）
    for d in sorted(b_dirs - a_dirs):
        plan.trashes.append(TrashOp(
            path=d,
            size=0,
            pair_index=pair_index,
            is_dir=True,
        ))


# ═══════════════════════════════════════════════════════════════════
# 路径匹配
# ═══════════════════════════════════════════════════════════════════

def _match_by_similarity(
    a_paths: list[str],
    b_paths: list[str],
) -> list[tuple[str, str]]:
    """用路径相似度做一对一匹配。

    优先精确匹配，然后按最长公共后缀匹配。
    返回 (a_path, b_path) 配对列表。
    """
    pairs: list[tuple[str, str]] = []
    remaining_b = list(b_paths)

    for ap in a_paths:
        # 精确匹配优先
        if ap in remaining_b:
            remaining_b.remove(ap)
            pairs.append((ap, ap))
            continue

        # 按相似度找最佳匹配
        if remaining_b:
            scores = [(bp, _path_similarity(ap, bp)) for bp in remaining_b]
            scores.sort(key=lambda x: x[1], reverse=True)
            best_bp, best_score = scores[0]
            if best_score > 0.3:  # 阈值
                remaining_b.remove(best_bp)
                pairs.append((ap, best_bp))
                continue

        # 无匹配 → 保守处理（由调用方处理为 Upload + Trash）
        # 这里不生成对，让 ap 落入 Upload，bp 落入 Trash

    return pairs


def _path_similarity(a: str, b: str) -> float:
    """计算两个路径的相似度（0~1）。

    以公共后缀长度占比为主，辅以 SequenceMatcher。
    """
    a_parts = a.replace("\\", "/").split("/")
    b_parts = b.replace("\\", "/").split("/")

    # 公共后缀长度
    common_suffix = 0
    for pa, pb in zip(reversed(a_parts), reversed(b_parts)):
        if pa == pb:
            common_suffix += 1
        else:
            break

    suffix_score = common_suffix / max(len(a_parts), len(b_parts), 1)

    # SequenceMatcher
    seq_score = SequenceMatcher(None, a, b).ratio()

    return max(suffix_score, seq_score)
