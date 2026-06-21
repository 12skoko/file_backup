"""HTTP 服务 + 客户端 — B 端常驻服务器，A 端通过 get_tree() 拉取文件树。

B 端扫描改为异步：/tree 立即返回 task_id，后台线程执行扫描，
A 端轮询 /tree/<task_id> 获取结果。服务器使用多线程模式，
扫描期间 /health 等其他请求不受影响。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

from .config import Config
from .scanner import scan


# ═══════════════════════════════════════════════════════════════════
# 异常
# ═══════════════════════════════════════════════════════════════════

class ServerError(Exception):
    """与 B 端服务器通信失败。"""
    pass


# ═══════════════════════════════════════════════════════════════════
# 扫描任务管理（B 端内存）
# ═══════════════════════════════════════════════════════════════════

_scan_tasks: dict[str, dict] = {}
_scan_lock = threading.Lock()
_SCAN_TASK_TTL_SECONDS = 600  # 任务结果保留 10 分钟，超时自动清理


def _cleanup_old_tasks() -> None:
    """清理过期的扫描任务结果。"""
    now = datetime.now()
    with _scan_lock:
        stale = [
            tid for tid, t in _scan_tasks.items()
            if t.get("finished_at") is not None
            and (now - t["finished_at"]).total_seconds() > _SCAN_TASK_TTL_SECONDS
        ]
        for tid in stale:
            del _scan_tasks[tid]


def _run_scan_async(config: Config, task_id: str) -> None:
    """在后台线程中执行扫描，完成后将结果写入 _scan_tasks。"""
    try:
        pairs = scan(config)
        data = {"pairs": [ps.to_dict() for ps in pairs]}
        with _scan_lock:
            _scan_tasks[task_id] = {
                "status": "done",
                "data": data,
                "finished_at": datetime.now(),
            }
    except Exception as exc:
        with _scan_lock:
            _scan_tasks[task_id] = {
                "status": "error",
                "error": str(exc),
                "finished_at": datetime.now(),
            }


# ═══════════════════════════════════════════════════════════════════
# A 端客户端
# ═══════════════════════════════════════════════════════════════════

def get_tree(
    target_url: str,
    token: str,
    timeout: int = 300,
    poll_interval: int = 2,
) -> list[dict]:
    """从 B 端拉取文件树（异步模式）。

    1. 调用 /tree 触发扫描，立即获取 task_id
    2. 轮询 /tree/<task_id> 直到扫描完成
    3. 返回 pairs 列表

    参数:
        timeout: 轮询总超时（秒），默认 5 分钟
        poll_interval: 轮询间隔（秒），默认 2 秒
    """
    base = target_url.rstrip("/")

    # ── 1. 触发扫描 ──
    req = Request(base + "/tree")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(req, timeout=30) as resp:
            trigger = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = _read_error_body(e)
        if e.code == 401:
            raise ServerError(f"认证失败：token 不正确 ({base})")
        raise ServerError(f"B 端返回 HTTP {e.code}: {body}")
    except URLError as e:
        raise ServerError(f"无法连接 B 端 ({base}): {e.reason}")
    except json.JSONDecodeError as e:
        raise ServerError(f"B 端返回数据格式错误: {e}")

    task_id = trigger.get("task_id")
    if not task_id:
        raise ServerError(f"B 端未返回 task_id，收到: {trigger}")

    # ── 2. 轮询等待扫描结果 ──
    status_url = f"{base}/tree/{task_id}"
    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(poll_interval)
        try:
            req2 = Request(status_url)
            req2.add_header("Authorization", f"Bearer {token}")
            with urlopen(req2, timeout=10) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except (URLError, OSError, json.JSONDecodeError):
            # 网络抖动，继续轮询
            continue

        if "pairs" in result:
            return result["pairs"]

        if result.get("status") == "error":
            raise ServerError(f"B 端扫描出错: {result.get('error')}")

        # status == "scanning"，继续等待

    raise ServerError(f"等待 B 端扫描超时 ({timeout}s)")


def check_health(target_url: str, token: str, timeout: int = 10) -> bool:
    """检查 B 端是否可达（异步模式下立即可用，不会被扫描阻塞）。"""
    url = target_url.rstrip("/") + "/health"
    req = Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body.get("status") == "ok"
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════
# 多线程 HTTP 服务器
# ═══════════════════════════════════════════════════════════════════

class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器。

    每个请求在独立线程中处理，扫描期间 /health、/tree/<id> 等
    其他请求不受影响。daemon_threads 确保主线程退出时自动清理。
    """
    daemon_threads = True


# ═══════════════════════════════════════════════════════════════════
# B 端 HTTP 请求处理器
# ═══════════════════════════════════════════════════════════════════

class _BackupHandler(BaseHTTPRequestHandler):
    """B 端请求处理器（多线程安全）。"""

    # 类变量，由 BackupServer.start() 在启动前设置一次
    config: Config = None  # type: ignore

    def do_GET(self) -> None:
        # ── Token 认证 ──
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/health":
            self._handle_health()
        elif path == "/tree":
            self._handle_tree()
        elif path.startswith("/tree/"):
            task_id = path[len("/tree/"):]
            self._handle_tree_status(task_id)
        else:
            self._send_error(404, "Not Found")

    def log_message(self, format, *args):
        """重定向日志到 stderr，避免混入 HTTP 输出。"""
        print(f"[server] {args[0]}", file=sys.stderr)

    # ── 认证 ──

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {self.config.server.token}"
        if auth != expected:
            self._send_error(401, '{"error":"unauthorized"}')
            return False
        return True

    # ── 路由处理 ──

    def _handle_health(self) -> None:
        """健康检查——始终立即可用。"""
        self._send_json({"status": "ok"})

    def _handle_tree(self) -> None:
        """触发异步扫描，立即返回 task_id。

        后台线程执行 scan()，期间 /health 和 /tree/<id> 不受影响。
        """
        _cleanup_old_tasks()

        task_id = uuid.uuid4().hex[:12]
        with _scan_lock:
            _scan_tasks[task_id] = {
                "status": "scanning",
                "started_at": datetime.now(),
            }

        t = threading.Thread(
            target=_run_scan_async,
            args=(self.config, task_id),
            daemon=True,
        )
        t.start()

        self._send_json({"task_id": task_id, "status": "scanning"})

    def _handle_tree_status(self, task_id: str) -> None:
        """查询扫描任务状态。

        返回:
            {"status": "scanning"}           — 仍在扫描中
            {"pairs": [...]}                 — 扫描完成，返回结果
            {"status": "error", "error":...} — 扫描出错
        """
        with _scan_lock:
            task = _scan_tasks.get(task_id)

        if task is None:
            self._send_error(404, json.dumps({"error": "task not found"}))
            return

        if task["status"] == "scanning":
            self._send_json({"status": "scanning"})
        elif task["status"] == "done":
            self._send_json(task["data"])
        else:
            self._send_error(500, json.dumps({"error": task.get("error", "unknown")}))

    # ── 响应辅助 ──

    def _send_json(self, data: dict | list) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code: int, message: str) -> None:
        body = message.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ═══════════════════════════════════════════════════════════════════
# 启动 B 端服务
# ═══════════════════════════════════════════════════════════════════

class BackupServer:
    """B 端常驻服务：HTTP（多线程）+ rclone WebDAV。"""

    def __init__(self, config: Config):
        self.config = config
        self._httpd: Optional[_ThreadingHTTPServer] = None
        self._rclone_proc: Optional[subprocess.Popen] = None
        self._shutting_down = False

    def start(self) -> None:
        """启动服务（阻塞）。"""
        # ── 启动 rclone serve webdav ──
        self._start_rclone()

        # ── 启动 HTTP（多线程模式）──
        _BackupHandler.config = self.config
        self._httpd = _ThreadingHTTPServer(
            ("0.0.0.0", self.config.server.port),
            _BackupHandler,
        )

        # 只注册 SIGTERM（Unix 下 kill 命令用），SIGINT 交给 KeyboardInterrupt
        try:
            signal.signal(signal.SIGTERM, self._on_sigterm)
        except (ValueError, OSError):
            pass  # Windows 不支持 SIGTERM，靠 KeyboardInterrupt

        print(f"[server] B 端服务已启动")
        print(f"  HTTP   : http://0.0.0.0:{self.config.server.port}  (文件树查询)")
        print(f"  WebDAV : http://0.0.0.0:{self.config.webdav.port}  (文件传输)")
        print(f"  扫描路径: {[p.source for p in self.config.paths]}")
        print(f"  WebDAV 根: {self.config.webdav.root}")
        print(f"  扫描模式: 异步（/tree 触发扫描 → /tree/<id> 轮询结果）")

        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[server] 收到 Ctrl+C，正在关闭...")
        finally:
            self.shutdown()

    def _on_sigterm(self, signum, frame):
        """SIGTERM 信号处理（仅 Unix）。"""
        print("\n[server] 收到 SIGTERM，正在关闭...")
        self.shutdown()

    def shutdown(self) -> None:
        """关闭服务（幂等，重复调用安全）。"""
        if self._shutting_down:
            return
        self._shutting_down = True

        # 1. 先关 HTTP
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

        # 2. 再关 rclone WebDAV 子进程
        if self._rclone_proc:
            print("[server] 正在关闭 rclone WebDAV...")
            try:
                self._rclone_proc.terminate()
                try:
                    self._rclone_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    print("[server] rclone 未响应，强制终止...")
                    self._rclone_proc.kill()
                    self._rclone_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("[server] rclone 强制终止失败，请手动检查")
            except Exception as exc:
                print(f"[server] 关闭 rclone 时出错: {exc}")
            finally:
                self._rclone_proc = None

        print("[server] 服务已关闭")

    def _start_rclone(self) -> None:
        """启动 rclone serve webdav 子进程。"""
        webdav_port = self.config.webdav.port
        webdav_root = self.config.webdav.root

        print(f"[server] 启动 rclone serve webdav (端口 {webdav_port})...")

        try:
            self._rclone_proc = subprocess.Popen(
                [
                    self.config.rclone.binary,
                    "serve", "webdav",
                    webdav_root,
                    "--addr", f":{webdav_port}",
                    "--vfs-cache-mode", "writes",
                    "--quiet",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            time.sleep(1.5)
            if self._rclone_proc.poll() is not None:
                stderr_raw = self._rclone_proc.stderr
                stderr = stderr_raw.read().decode("utf-8", errors="replace") if stderr_raw else ""
                print(f"[WARN] rclone serve webdav 未能启动: {stderr}")
        except FileNotFoundError:
            print(f"[WARN] 找不到 rclone 二进制文件 '{self.config.rclone.binary}'，"
                  f"WebDAV 未启动。文件传输将不可用。")
        except Exception as exc:
            print(f"[WARN] 启动 rclone serve webdav 失败: {exc}")


# ═══════════════════════════════════════════════════════════════════
# 工具
# ═══════════════════════════════════════════════════════════════════

def _read_error_body(e: HTTPError) -> str:
    """安全读取 HTTP 错误响应体。"""
    try:
        return e.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
