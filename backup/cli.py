"""命令行入口 — backup serve / diff / sync。"""

from __future__ import annotations

import argparse
import sys
import time

from .config import load_source_config, load_target_config
from .scanner import scan, PairScan
from .differ import compare
from .reporter import (
    build_diff_report,
    save_diff_report,
    format_diff_text,
    DiffReport,
    SyncReport,
    save_sync_report,
    format_sync_text,
)
from .server import get_tree, check_health, ServerError, BackupServer
from .executor import run_plan


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="backup",
        description="异地文件备份系统 — A 地 → B 地单向同步",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # backup serve
    p_serve = sub.add_parser("serve", help="启动 B 端常驻服务")
    p_serve.add_argument("--config", "-c", default="config.target.yaml", help="配置文件路径")

    # backup diff
    p_diff = sub.add_parser("diff", help="扫描 + 对比，输出差异报告（不执行同步）")
    p_diff.add_argument("--config", "-c", default="config.source.yaml", help="配置文件路径")

    # backup sync
    p_sync = sub.add_parser("sync", help="扫描 + 对比 + 确认 + 执行同步")
    p_sync.add_argument("--config", "-c", default="config.source.yaml", help="配置文件路径")
    p_sync.add_argument("--yes", "-y", action="store_true", help="跳过确认，直接执行")

    args = parser.parse_args(argv)

    if args.command == "serve":
        cmd_serve(args)
    elif args.command == "diff":
        cmd_diff(args)
    elif args.command == "sync":
        cmd_sync(args)


# ═══════════════════════════════════════════════════════════════════
# serve（B 端）
# ═══════════════════════════════════════════════════════════════════

def cmd_serve(args) -> None:
    cfg = load_target_config(args.config)
    server = BackupServer(cfg)
    server.start()


# ═══════════════════════════════════════════════════════════════════
# diff（A 端预览）
# ═══════════════════════════════════════════════════════════════════

def cmd_diff(args) -> None:
    cfg = load_source_config(args.config)

    # 1. 扫描 A 端
    print("扫描 A 端...")
    scan_a = scan(cfg)
    _print_scan_summary(scan_a, "A")

    # 2. 拉取 B 端树
    print(f"连接 B 端 {cfg.target.url} ...")
    if not check_health(cfg.target.url, cfg.target.token):
        print(f"[ERROR] B 端不可达: {cfg.target.url}")
        sys.exit(1)

    print("拉取 B 端文件树...")
    try:
        b_pairs_raw = get_tree(cfg.target.url, cfg.target.token)
    except ServerError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    scan_b = [PairScan.from_dict(d) for d in b_pairs_raw]
    _print_scan_summary(scan_b, "B")

    # 3. 对比
    print("对比差异...")
    plan = compare(scan_a, scan_b)

    # 4. 报告
    report = build_diff_report(plan)

    # 保存 JSON
    filepath = save_diff_report(report, cfg.sync.report_dir)

    # 输出文本
    print()
    print(format_diff_text(report))
    print(f"报告已保存: {filepath}")

    if plan.is_empty:
        print("两边一致，无需同步。")


# ═══════════════════════════════════════════════════════════════════
# sync（A 端执行）
# ═══════════════════════════════════════════════════════════════════

def cmd_sync(args) -> None:
    cfg = load_source_config(args.config)

    # 1. 扫描 A 端
    print("扫描 A 端...")
    scan_a = scan(cfg)
    _print_scan_summary(scan_a, "A")

    # 2. 拉取 B 端树
    print(f"连接 B 端 {cfg.target.url} ...")
    if not check_health(cfg.target.url, cfg.target.token):
        print(f"[ERROR] B 端不可达: {cfg.target.url}")
        sys.exit(1)

    print("拉取 B 端文件树...")
    try:
        b_pairs_raw = get_tree(cfg.target.url, cfg.target.token)
    except ServerError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    scan_b = [PairScan.from_dict(d) for d in b_pairs_raw]
    _print_scan_summary(scan_b, "B")

    # 3. 对比
    print("对比差异...")
    plan = compare(scan_a, scan_b)

    # 4. 输出 diff 报告
    diff_report = build_diff_report(plan)
    print()
    print(format_diff_text(diff_report))

    if plan.is_empty:
        print("两边一致，无需同步。")
        return

    # 5. 确认
    if not args.yes:
        print()
        try:
            answer = input("确认执行同步? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            sys.exit(0)
        if answer not in ("y", "yes"):
            print("已取消。")
            sys.exit(0)

    # 6. 执行
    print()
    print("开始同步...")
    t0 = time.time()

    sync_result = run_plan(plan, cfg)

    elapsed = time.time() - t0

    # 7. 同步报告
    sync_report = SyncReport(
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        duration_sec=elapsed,
        diff=diff_report.to_dict(),
        results={
            "uploaded": sync_result.uploaded,
            "moved": sync_result.moved,
            "trashed": sync_result.trashed,
            "mkdirs": sync_result.mkdirs,
            "errors": sync_result.errors,
        },
    )

    # 保存
    filepath = save_sync_report(sync_report, cfg.sync.report_dir)

    # 输出
    print()
    print(format_sync_text(diff_report, sync_result, elapsed))
    print(f"报告已保存: {filepath}")


# ═══════════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════════

def _print_scan_summary(pairs: list[PairScan], label: str) -> None:
    total_files = sum(len(ps.files) for ps in pairs)
    total_dirs = sum(len(ps.dirs) for ps in pairs)
    print(f"  [{label}端] {total_files} 个文件, {total_dirs} 个目录")


if __name__ == "__main__":
    main()
