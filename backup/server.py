"""HTTP 服务 + 客户端 — B 端常驻服务器，A 端通过 get_tree() 拉取文件树。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
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
# A 端客户端
# ═══════════════════════════════════════════════════════════════════

def get_tree(target_url: str, token: str, timeout: int = 30) -> list[dict]:
    """从 B 端拉取文件树。

    返回 list[dict]，每个元素对应一个 path pair 的扫描结果。
    """
    url = target_url.rstrip("/") + "/tree"
    req = Request(url)
    req.add_header("Authorization", f"Bearer {token}")

    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        if e.code == 401:
            raise ServerError(f"认证失败：token 不正确 ({url})")
        raise ServerError(f"B 端返回 HTTP {e.code}: {body}")
    except URLError as e:
        raise ServerError(f"无法连接 B 端 ({url}): {e.reason}")
    except json.JSONDecodeError as e:
        raise ServerError(f"B 端返回数据格式错误: {e}")

    if not isinstance(data, dict) or "pairs" not in data:
        raise ServerError(f"B 端返回数据缺少 'pairs' 字段")

    return data["pairs"]


def check_health(target_url: str, token: str, timeout: int = 10) -> bool:
    """检查 B 端是否可达。"""
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
# B 端 HTTP 服务器
# ═══════════════════════════════════════════════════════════════════

class _BackupHandler(BaseHTTPRequestHandler):
    """B 端请求处理器。"""

    # 类变量，由 serve() 初始化
    config: Config = None  # type: ignore

    def do_GET(self) -> None:
        # ── Token 认证 ──
        if not self._check_auth():
            return

        parsed = urlparse(self.path)

        if parsed.path == "/health":
            self._handle_health()
        elif parsed.path == "/tree":
            self._handle_tree()
        else:
            self._send_error(404, "Not Found")

    def log_message(self, format, *args):
        """重定向日志到 stderr，避免混入 HTTP 输出。"""
        print(f"[server] {args[0]}", file=sys.stderr)

    # ── 内部 ──

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {self.config.server.token}"
        if auth != expected:
            self._send_error(401, '{"error":"unauthorized"}')
            return False
        return True

    def _handle_health(self) -> None:
        self._send_json({"status": "ok"})

    def _handle_tree(self) -> None:
        try:
            pairs = scan(self.config)
            data = {"pairs": [ps.to_dict() for ps in pairs]}
            self._send_json(data)
        except Exception as exc:
            self._send_error(500, json.dumps({"error": str(exc)}))

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
    """B 端常驻服务：HTTP + rclone WebDAV。"""

    def __init__(self, config: Config):
        self.config = config
        self._httpd: Optional[HTTPServer] = None
        self._rclone_proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        """启动服务（阻塞）。"""
        if not self.config.is_target:
            print("[ERROR] serve 命令只能在 target 模式下使用")
            sys.exit(1)

        # ── 启动 rclone serve webdav ──
        self._start_rclone()

        # ── 启动 HTTP ──
        _BackupHandler.config = self.config
        self._httpd = HTTPServer(
            ("0.0.0.0", self.config.server.port),
            _BackupHandler,
        )

        # 注册退出清理
        def cleanup(signum=None, frame=None):
            print("\n[server] 正在关闭...")
            self.shutdown()

        signal.signal(signal.SIGINT, cleanup)
        signal.signal(signal.SIGTERM, cleanup)

        webdav_port = self._webdav_port
        print(f"[server] B 端服务已启动")
        print(f"  HTTP   : http://0.0.0.0:{self.config.server.port}  (文件树查询)")
        print(f"  WebDAV : http://0.0.0.0:{webdav_port}  (文件传输)")
        print(f"  扫描路径: {[p.source for p in self.config.paths]}")
        print(f"  WebDAV 根: {self.config.webdav.root}")

        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        """关闭服务。"""
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

        if self._rclone_proc:
            self._rclone_proc.terminate()
            try:
                self._rclone_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._rclone_proc.kill()
                self._rclone_proc.wait()
            self._rclone_proc = None

    @property
    def _webdav_port(self) -> int:
        """WebDAV 端口 = HTTP 端口 + 1。"""
        return self.config.server.port + 1

    def _start_rclone(self) -> None:
        """启动 rclone serve webdav 子进程。

        HTTP 和 WebDAV 协议不同，必须使用不同端口。
        WebDAV 端口 = server.port + 1（如 HTTP:9527 → WebDAV:9528）。
        A 端配置 rclone remote 时应指向此端口。
        """
        webdav_port = self._webdav_port
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
            # 稍等一下看是否启动成功
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
