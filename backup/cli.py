from __future__ import annotations

from pathlib import Path
import argparse
import time

from .config import load_source_config, load_target_config
from .differ import compare
from .executor import run_plan
from .ignore import load_ignore_rules
from .reporter import make_diff_report, make_sync_report, print_report, save_report
from .scanner import scan_paths
from .server import serve, start_scan, wait_for_scan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backup")
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="start target scan API and WebDAV service")
    serve_parser.add_argument("--config", default="target.yaml")

    diff_parser = sub.add_parser("diff", help="scan and print diff report")
    diff_parser.add_argument("--config", default="source.yaml")
    diff_parser.add_argument("--target-config", help="optional local target.yaml for exact WebDAV paths")

    sync_parser = sub.add_parser("sync", help="scan, confirm, and execute sync")
    sync_parser.add_argument("--config", default="source.yaml")
    sync_parser.add_argument("--target-config", help="optional local target.yaml for exact WebDAV paths")
    sync_parser.add_argument("--yes", "-y", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "serve":
        serve(args.config)
        return 0
    if args.command == "diff":
        plan, source, _, diff_report, _, _ = _build_plan(args.config, args.target_config)
        print_report(diff_report)
        path = save_report(diff_report, source.report_dir)
        print(f"report saved: {path}")
        return 0
    if args.command == "sync":
        plan, source, target, diff_report, target_roots, trash_root = _build_plan(args.config, args.target_config)
        print_report(diff_report)
        save_report(diff_report, source.report_dir)
        if not args.yes:
            answer = input("Execute sync? [y/N] ").strip().lower()
            if answer not in {"y", "yes"}:
                print("cancelled")
                return 1
        start = time.monotonic()
        results = run_plan(plan, source, target, target_roots=target_roots, trash_root=trash_root)
        sync_report = make_sync_report(diff_report, results, time.monotonic() - start)
        print_report(sync_report)
        path = save_report(sync_report, source.report_dir)
        print(f"report saved: {path}")
        return 0 if not results["errors"] else 2
    return 2


def _build_plan(config_path: str, target_config_path: str | None):
    source = load_source_config(config_path)
    target = load_target_config(target_config_path) if target_config_path else None
    ignore = load_ignore_rules(source.exclude_file)
    scan_a = scan_paths(source.paths, source.cache_dir, ignore)
    job_id = start_scan(source.api_url, source.token)
    scan_b = wait_for_scan(
        source.api_url,
        source.token,
        job_id,
        source.scan_poll_interval_sec,
        source.scan_timeout_sec,
    )
    plan = compare(scan_a, scan_b)
    diff_report = make_diff_report(plan)
    return plan, source, target, diff_report, scan_b.target_roots, scan_b.trash_root


if __name__ == "__main__":
    raise SystemExit(main())
