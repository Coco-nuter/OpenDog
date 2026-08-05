# PC B 事件镜像读取器

该目录只负责从 OpenDog 服务器增量下载事件并保存为本地 JSONL，不进行
实时监听、状态切割或状态分析。

## 文件说明

```text
pc_b_reader/
├─ reader.py                 一次性增量下载程序
├─ config.json               本机配置和 token，不提交到 Git
├─ config.example.json       配置模板
├─ last_seq.cursor           自动生成，最后成功保存的服务器 seq
├─ mirror_events.jsonl       自动生成，本地事件镜像
└─ logs/
   └─ reader.log             自动生成，运行日志
```

## 前置条件

服务器必须部署包含 `GET /events` 的新版 `server/app/main.py`。如果服务器
前面使用 Nginx，也必须同步新版 `server/deploy/nginx-opendog.conf`。

该程序只使用 Python 标准库，建议使用 Python 3.10 或更高版本，不需要
安装额外依赖。

## 配置

编辑 `config.json`：

```json
{
  "server_url": "http://SERVER_IP:8899",
  "token": "与服务器OPENDOG_TOKEN相同的token",
  "mirror_file": "mirror_events.jsonl",
  "cursor_file": "last_seq.cursor",
  "log_file": "logs/reader.log",
  "batch_size": 500,
  "request_timeout_seconds": 15,
  "max_retries": 5,
  "max_retry_seconds": 60,
  "use_proxy": false
}
```

如果服务器使用 HTTPS，`server_url` 填写域名，例如：

```json
"server_url": "https://ingest.example.com"
```

## 运行

在 PC B 的 `pc_b_reader` 目录执行：

```powershell
python reader.py --config config.json
```

reader 会执行以下流程：

```text
读取 last_seq.cursor
        ↓
GET /events?after_seq=<last_seq>&limit=500
        ↓
按 seq 升序追加到 mirror_events.jsonl
        ↓
每成功写入一条后原子更新 last_seq.cursor
        ↓
继续拉取下一页
        ↓
没有更多事件后正常退出
```

该版本不会保持运行，也不会连接 `/stream`。需要更新本地镜像时再次执行同一
命令即可，它只下载 `seq > last_seq` 的新增事件。

成功日志示例：

```text
INFO Pulling events after seq 0
INFO Saved 500 events in this page through seq 500
INFO Saved 83 events in this page through seq 583
INFO Mirror is up to date at seq 583; downloaded 583 events
```

## 本地镜像格式

`mirror_events.jsonl` 每行是一个完整事件：

```json
{"seq":1,"event_id":"...","source":"pc_a","device_id":"windows_pc_a","type":"focused_window_ocr","ts":1782360000.123,"data":{"timestamp":"2026-06-25 10:00:00.123","trigger":"focus_switch","focus_id":"chrome.exe:123","app":"chrome.exe","title":"Google Chrome","text":"OCR内容"},"received_at":"2026-06-25T02:00:01.000+00:00"}
```

后续 OpenClaw 或分析程序可以只读取这个本地文件。

## 重新下载全部数据

这会删除 PC B 的本地镜像，不会删除服务器数据：

```powershell
Remove-Item .\last_seq.cursor -ErrorAction SilentlyContinue
Remove-Item .\mirror_events.jsonl -ErrorAction SilentlyContinue
python reader.py --config config.json
```

必须同时删除 cursor 和镜像。只删除其中一个会触发一致性保护并停止运行。

## 故障处理

- `401/403`：PC B token 与服务器 `OPENDOG_TOKEN` 不一致。
- `404`：服务器尚未部署新版 `/events` 接口，或 Nginx 未放行 `/events`。
- `timed out`：检查服务器、防火墙、安全组；若系统代理干扰，保持
  `use_proxy: false`。
- 下载中断：重新运行同一命令，会从最后成功写入的 seq 继续。
