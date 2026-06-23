# 异地文件备份系统

A 到 B 的单向备份工具。Python 负责扫描、diff 和报告，rclone 负责文件传输。

## 命令

```powershell
python -m backup serve --config target.yaml
python -m backup diff --config source.yaml
python -m backup sync --config source.yaml
```

`sync` 默认会先输出 diff 报告并等待确认，使用 `--yes` 可跳过确认。

## 依赖

- Python 3.10+
- `pyyaml`
- `rclone` 在 PATH 中，且 A 端已配置好指向 B 端 WebDAV 的 remote
