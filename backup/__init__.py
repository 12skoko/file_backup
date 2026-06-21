"""异地文件备份系统 — A 地 → B 地单向同步。

A 端按需执行 backup diff（预览）或 backup sync（执行）。
B 端 backup serve 常驻后台，响应 /tree 请求 + 暴露 WebDAV。
"""

__version__ = "0.1.0"
