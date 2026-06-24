import tempfile
import unittest
from pathlib import Path

from backup.config import SourceConfig, TargetConfig, load_target_config
from backup.executor import run_plan
from backup.models import FileEntry, MkdirOp, Plan, ScanResult, UploadOp

try:
    import yaml  # noqa: F401
except ModuleNotFoundError:
    yaml = None


class ConfigExecutorTests(unittest.TestCase):
    def test_target_config_treats_target_and_trash_as_webdav_relative_paths(self):
        if yaml is None:
            self.skipTest("PyYAML is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            webdav_root = tmp_path / "backup"
            webdav_root.mkdir()
            config = tmp_path / "target.yaml"
            config.write_text(
                f"""
server:
  host: 127.0.0.1
  port: 9527
  token: t
webdav:
  host: 127.0.0.1
  port: 9528
  root: {webdav_root.as_posix()}
paths:
  - name: backup1
    target: /backup1
trash:
  dir: /.backup_trash
""",
                encoding="utf-8",
            )

            target = load_target_config(config)

            self.assertEqual(target.paths[0].target, webdav_root / "backup1")
            self.assertEqual(target.trash_dir, webdav_root / ".backup_trash")

    def test_executor_dry_run_maps_target_webdav_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src_root = tmp_path / "a_photos"
            target_root = tmp_path / "backup"
            b_photos = target_root / "photos"
            for path in [src_root, b_photos]:
                path.mkdir(parents=True)
            (src_root / "x.txt").write_text("x", encoding="utf-8")

            source = SourceConfig(
                path=tmp_path / "source.yaml",
                token="t",
                paths=[type("SourcePathLike", (), {"name": "photos", "source": src_root})()],
                exclude_file=tmp_path / ".backupignore",
                cache_dir=tmp_path / "cache_a",
                api_url="http://127.0.0.1:9527",
                rclone_remote="B",
                scan_poll_interval_sec=2,
                scan_timeout_sec=3600,
                rclone_binary="rclone",
                rclone_retries=3,
                rclone_transfers=4,
                dry_run=True,
                report_dir=tmp_path / "reports",
            )
            target = TargetConfig(
                path=tmp_path / "target.yaml",
                host="127.0.0.1",
                port=9527,
                token="t",
                webdav_host="127.0.0.1",
                webdav_port=9528,
                webdav_root=target_root,
                paths=[type("TargetPathLike", (), {"name": "photos", "target": b_photos})()],
                exclude_file=tmp_path / ".backupignore",
                cache_dir=tmp_path / "cache_b",
                trash_dir=target_root / ".backup_trash",
                trash_keep_days=30,
                scan_max_workers=1,
                job_ttl_sec=3600,
                result_ttl_sec=3600,
                graceful_timeout_sec=10,
                rclone_binary="rclone",
            )
            plan = Plan(uploads=[UploadOp("photos/x.txt", 1)], mkdirs=[MkdirOp("photos/newdir")])
            results = run_plan(plan, source, target)

            self.assertEqual(results["mkdirs"][0]["command"][-1], "B:photos/newdir")
            self.assertEqual(results["uploaded"][0]["command"][2], str(src_root / "x.txt"))
            self.assertEqual(results["uploaded"][0]["command"][3], "B:photos/x.txt")

    def test_executor_uses_target_roots_from_remote_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            src_root = tmp_path / "source"
            src_root.mkdir()
            (src_root / "x.txt").write_text("x", encoding="utf-8")
            source = SourceConfig(
                path=tmp_path / "source.yaml",
                token="t",
                paths=[type("SourcePathLike", (), {"name": "backup1", "source": src_root})()],
                exclude_file=tmp_path / ".backupignore",
                cache_dir=tmp_path / "cache_a",
                api_url="http://127.0.0.1:9527",
                rclone_remote="B",
                scan_poll_interval_sec=2,
                scan_timeout_sec=3600,
                rclone_binary="rclone",
                rclone_retries=3,
                rclone_transfers=4,
                dry_run=True,
                report_dir=tmp_path / "reports",
            )
            plan = Plan(
                uploads=[UploadOp("backup1/x.txt", 1)],
                mkdirs=[MkdirOp("backup1/nested")],
            )
            results = run_plan(plan, source, target_roots={"backup1": "backup1"}, trash_root=".backup_trash")

            self.assertEqual(results["mkdirs"][0]["command"][-1], "B:backup1/nested")
            self.assertEqual(results["uploaded"][0]["command"][3], "B:backup1/x.txt")

    def test_scan_result_from_dict_rebuilds_file_keys_from_entries(self):
        result = ScanResult.from_dict(
            {
                "files": {
                    "/x.txt": {
                        "name": "photos",
                        "rel_path": "x.txt",
                        "size": 1,
                        "mtime": 1,
                        "hash": "h",
                    }
                },
                "dirs": [],
            }
        )

        self.assertEqual(
            result.files,
            {"photos/x.txt": FileEntry("photos", "x.txt", 1, 1, "h")},
        )

    def test_scan_result_from_dict_preserves_target_roots(self):
        result = ScanResult.from_dict(
            {
                "files": {},
                "dirs": [],
                "target_roots": {"backup1": "/backup1/"},
                "trash_root": "/.backup_trash/",
            }
        )

        self.assertEqual(result.target_roots, {"backup1": "backup1"})
        self.assertEqual(result.trash_root, ".backup_trash")


if __name__ == "__main__":
    unittest.main()
