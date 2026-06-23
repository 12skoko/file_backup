from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import subprocess
import threading
import time
import uuid

from .config import TargetConfig, load_target_config
from .ignore import load_ignore_rules
from .models import ScanResult
from .scanner import scan_paths


@dataclass
class ScanJob:
    job_id: str
    status: str = "queued"
    error: str | None = None
    result: ScanResult | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    future: Future | None = None


class ScanService:
    def __init__(self, config: TargetConfig):
        self.config = config
        self.executor = ThreadPoolExecutor(max_workers=max(1, config.scan_max_workers))
        self.jobs: dict[str, ScanJob] = {}
        self.lock = threading.Lock()
        self.rclone_proc: subprocess.Popen | None = None
        self.httpd: ThreadingHTTPServer | None = None

    def start_scan(self) -> ScanJob:
        with self.lock:
            for job in self.jobs.values():
                if job.status in {"queued", "running"}:
                    return job
            job = ScanJob(job_id=uuid.uuid4().hex)
            future = self.executor.submit(self._run_scan, job.job_id)
            job.future = future
            self.jobs[job.job_id] = job
            return job

    def get_job(self, job_id: str) -> ScanJob | None:
        with self.lock:
            return self.jobs.get(job_id)

    def cleanup_old_jobs(self) -> None:
        cutoff = time.time() - self.config.result_ttl_sec
        with self.lock:
            for job_id, job in list(self.jobs.items()):
                if job.status in {"done", "failed"} and job.updated_at < cutoff:
                    self.jobs.pop(job_id, None)

    def start_rclone(self) -> None:
        addr = f"{self.config.webdav_host}:{self.config.webdav_port}"
        cmd = [self.config.rclone_binary, "serve", "webdav", "--addr", addr, str(self.config.webdav_root)]
        self.rclone_proc = subprocess.Popen(cmd)

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)
        proc = self.rclone_proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=self.config.graceful_timeout_sec)
            except subprocess.TimeoutExpired:
                proc.kill()

    def _run_scan(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job.status = "running"
            job.updated_at = time.time()
        try:
            ignore = load_ignore_rules(self.config.exclude_file)
            result = scan_paths(self.config.paths, self.config.cache_dir, ignore)
            with self.lock:
                job.result = result
                job.status = "done"
                job.updated_at = time.time()
        except Exception as exc:
            with self.lock:
                job.error = str(exc)
                job.status = "failed"
                job.updated_at = time.time()


def serve(config_path: str | Path) -> None:
    config = load_target_config(config_path)
    service = ScanService(config)
    service.start_rclone()
    handler = _make_handler(service, config.token)
    httpd = ThreadingHTTPServer((config.host, config.port), handler)
    service.httpd = httpd
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
        service.shutdown()


def start_scan(api_url: str, token: str) -> str:
    data = _request_json(f"{api_url.rstrip('/')}/scan", token, method="POST")
    return str(data["job_id"])


def get_scan_status(api_url: str, token: str, job_id: str) -> dict[str, Any]:
    return _request_json(f"{api_url.rstrip('/')}/scan/{job_id}", token)


def get_scan_result(api_url: str, token: str, job_id: str) -> ScanResult:
    data = _request_json(f"{api_url.rstrip('/')}/scan/{job_id}/result", token)
    return ScanResult.from_dict(data)


def wait_for_scan(api_url: str, token: str, job_id: str, poll_interval: float, timeout: float) -> ScanResult:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = get_scan_status(api_url, token, job_id)
        if status["status"] == "done":
            return get_scan_result(api_url, token, job_id)
        if status["status"] == "failed":
            raise RuntimeError(status.get("error") or "remote scan failed")
        time.sleep(poll_interval)
    raise TimeoutError(f"remote scan timed out after {timeout} seconds")


def _request_json(url: str, token: str, method: str = "GET") -> dict[str, Any]:
    req = Request(url, method=method, headers={"Authorization": f"Bearer {token}"})
    try:
        with urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


def _make_handler(service: ScanService, token: str):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if not self._authorized():
                return self._json({"error": "unauthorized"}, status=401)
            service.cleanup_old_jobs()
            if self.path == "/health":
                return self._json({"status": "ok"})
            parts = self.path.strip("/").split("/")
            if len(parts) == 2 and parts[0] == "scan":
                job = service.get_job(parts[1])
                if not job:
                    return self._json({"error": "job not found"}, status=404)
                return self._json({"job_id": job.job_id, "status": job.status, "error": job.error})
            if len(parts) == 3 and parts[0] == "scan" and parts[2] == "result":
                job = service.get_job(parts[1])
                if not job:
                    return self._json({"error": "job not found"}, status=404)
                if job.status != "done" or not job.result:
                    return self._json({"error": "result not ready", "status": job.status}, status=409)
                return self._json(job.result.to_dict())
            return self._json({"error": "not found"}, status=404)

        def do_POST(self) -> None:
            if not self._authorized():
                return self._json({"error": "unauthorized"}, status=401)
            if self.path == "/scan":
                job = service.start_scan()
                return self._json({"job_id": job.job_id, "status": job.status})
            return self._json({"error": "not found"}, status=404)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _authorized(self) -> bool:
            return self.headers.get("Authorization") == f"Bearer {token}"

        def _json(self, payload: dict[str, Any], status: int = 200) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler
