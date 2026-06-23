from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
import json

from .models import Plan


def make_diff_report(plan: Plan) -> dict[str, Any]:
    return {
        "type": "diff",
        "timestamp": _timestamp(),
        "summary": {
            "will_upload": len(plan.uploads),
            "will_move": len(plan.moves),
            "will_trash": len(plan.trashes),
            "will_mkdir": len(plan.mkdirs),
            "extra_on_target_files": len(plan.extra_files),
            "extra_on_target_dirs": len(plan.extra_dirs),
            "total_bytes_upload": sum(op.size for op in plan.uploads),
        },
        "uploads": [asdict(op) for op in plan.uploads],
        "moves": [asdict(op) for op in plan.moves],
        "trashes": [asdict(op) for op in plan.trashes],
        "mkdirs": [op.path for op in plan.mkdirs],
        "extra_files": [asdict(op) for op in plan.extra_files],
        "extra_dirs": plan.extra_dirs,
    }


def make_sync_report(diff_report: dict[str, Any], results: dict[str, Any], duration_sec: float) -> dict[str, Any]:
    return {
        "type": "sync",
        "timestamp": _timestamp(),
        "duration_sec": round(duration_sec, 3),
        "diff": diff_report,
        "results": results,
    }


def save_report(report: dict[str, Any], report_dir: Path) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{report['type']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    path = report_dir / filename
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def print_report(report: dict[str, Any]) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")
