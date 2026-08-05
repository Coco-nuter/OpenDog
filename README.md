# OpenDog 总流程说明

OpenDog 当前采用三端架构：

```text
PC A 采集端
  focused_app_history_ocr.py
  syncer.py
        |
        | HTTP POST /ingest
        | TCP 8899
        v
服务器
  FastAPI: server/app/main.py
  SQLite: /var/lib/opendog-ingest/events.sqlite3
        ^
        | HTTP GET /events
        | TCP 8899
        |
PC B 读取端
  pc_b_reader/reader.py
```

PC A 负责生产数据，服务器负责保存和分发数据，PC B 负责读取数据。PC A 和 PC B 不直接通信。

## 1. PC A 采集流程

PC A 上运行两个程序：

```text
focused_app_history_ocr.py  负责采集
syncer.py                   负责上传
```

采集程序监听当前前台窗口、用户交互、截图变化和 OCR 结果，然后把事件追加写入本地 JSONL 文件：

```text
focus_history/history.jsonl
```

同时也会写入对应窗口自己的历史文件：

```text
focus_history/<app_focus>/history.jsonl
```

当前写入的事件字段只保留：

```json
{
  "event_id": "uuid",
  "source": "pc_a",
  "type": "focused_window_ocr",
  "timestamp": "2026-07-08 12:00:00.123",
  "trigger": "focus_switch",
  "focus_id": "chrome.exe:123456",
  "app": "chrome.exe",
  "title": "窗口标题",
  "text": "OCR 文本"
}
```

如果不想保存截图，运行采集程序时加：

```powershell
python focused_app_history_ocr.py --no-save-images
```

## 2. PC A 上传流程

`syncer.py` 读取 `sync_config.json`，持续监控：

```text
focus_history/history.jsonl
```

它不会重复上传整个文件，而是通过：

```text
sync_state/offset.cursor
```

记录已经成功上传到哪个字节位置。

上传流程：

```text
读取 offset.cursor
        ↓
从 history.jsonl 的 offset 位置继续读
        ↓
只处理完整 JSONL 行
        ↓
按批次 POST 到服务器 /ingest
        ↓
服务器返回成功
        ↓
更新 offset.cursor
```

只有服务器确认成功后，`syncer.py` 才会更新 `offset.cursor`。如果上传失败，cursor 不推进，下次会从同一位置重试，避免丢数据。

PC A 上传使用：

```http
POST http://服务器IP:8899/ingest
Authorization: Bearer <token>
Content-Type: application/json
```

请求体大致为：

```json
{
  "source": "pc_a",
  "device_id": "windows_pc_a",
  "events": [
    {
      "event_id": "uuid",
      "type": "focused_window_ocr",
      "ts": 1782360000.123,
      "data": {
        "timestamp": "2026-07-08 12:00:00.123",
        "trigger": "focus_switch",
        "focus_id": "chrome.exe:123456",
        "app": "chrome.exe",
        "title": "窗口标题",
        "text": "OCR 文本"
      }
    }
  ]
}
```

运行：

```powershell
python syncer.py --config sync_config.json
```

## 3. 服务器流程

服务器运行 FastAPI 服务：

```text
server/app/main.py
```

默认监听：

```text
0.0.0.0:8899
```

主要接口：

```text
GET  /health   健康检查
POST /ingest   接收 PC A 或其他设备上传的事件
GET  /events   给 PC B 拉取事件
```

服务器把事件保存到 SQLite：

```text
/var/lib/opendog-ingest/events.sqlite3
```

每条事件进入数据库后，服务器会分配一个递增的 `seq`：

```text
seq=1
seq=2
seq=3
...
```

`event_id` 用于去重。如果 PC A 上传成功后还没来得及更新 cursor 就崩溃，重启后可能重复上传同一事件，服务器会根据 `event_id` 避免重复写入。

查看服务器服务状态：

```bash
sudo systemctl status opendog-ingest --no-pager
sudo journalctl -u opendog-ingest -n 100 --no-pager
```

查看最近记录：

```bash
sudo sqlite3 /var/lib/opendog-ingest/events.sqlite3 \
'SELECT seq,event_id,source,device_id,event_type,event_ts FROM events ORDER BY seq DESC LIMIT 20;'
```

## 4. PC B 读取流程

PC B 运行：

```text
pc_b_reader/reader.py
```

它从服务器增量拉取事件，并保存成本地镜像：

```text
pc_b_reader/mirror_events.jsonl
```

PC B 使用：

```text
pc_b_reader/last_seq.cursor
```

记录自己已经处理到服务器的哪个 `seq`。

读取流程：

```text
读取 last_seq.cursor
        ↓
GET /events?after_seq=<last_seq>&limit=<batch_size>
        ↓
按 seq 升序写入 mirror_events.jsonl
        ↓
每成功写入一条，更新 last_seq.cursor
        ↓
继续拉下一页
        ↓
没有更多事件后退出
```

PC B 拉取使用：

```http
GET http://服务器IP:8899/events?after_seq=0&limit=500
Authorization: Bearer <token>
```

运行：

```powershell
cd pc_b_reader
python reader.py --config config.json
```

当前 PC B 版本只负责把 JSON 拉到本地，不做实时分析、不做状态切割，也不连接 `/stream`。

## 5. 多设备上传

当前架构可以支持 PC A、手机、其他电脑同时向服务器上传事件。

要求：

- 都上传到同一个接口：`POST /ingest`
- 每个设备使用不同的 `source` 和 `device_id`
- 每条事件必须有全局唯一 `event_id`
- 服务器统一用 `seq` 排序

示例：

```text
PC A   source=pc_a      device_id=windows_pc_a
手机   source=phone     device_id=android_phone
PC C   source=pc_c      device_id=windows_pc_c
```

PC B 不需要知道数据来自哪台设备，它只需要继续按 `seq` 拉取：

```text
GET /events?after_seq=<last_seq>
```

## 6. 端口和通信关系

当前默认端口：

```text
服务器 TCP 8899
```

通信关系：

```text
PC A -> 服务器: POST /ingest
PC B -> 服务器: GET /events
PC A 和 PC B: 不直接通信
```

上传和拉取使用同一个 TCP 端口是正常的。它们属于同一个 HTTP 服务，只是路径和方法不同。

如果后续使用 Nginx 和 HTTPS，外部客户端通常访问：

```text
TCP 443
```

Nginx 再把请求转发到本机内部的：

```text
127.0.0.1:8899
```

## 7. 推荐启动顺序

第一次部署或联调时，建议按这个顺序：

```text
1. 启动服务器 opendog-ingest
2. 用 GET /health 确认服务器可访问
3. 在 PC A 启动 focused_app_history_ocr.py
4. 在 PC A 启动 syncer.py
5. 确认服务器 SQLite 中出现记录
6. 在 PC B 运行 reader.py
7. 确认 PC B 生成 mirror_events.jsonl
```

常用检查命令：

```bash
curl http://127.0.0.1:8899/health
```

```bash
curl -H "Authorization: Bearer <token>" \
"http://127.0.0.1:8899/events?after_seq=0&limit=1"
```

## 8. 数据可靠性原则

整个链路遵守三个原则：

1. 本地先落盘，再上传。
2. 上传成功后才推进 PC A 的 `offset.cursor`。
3. PC B 写入本地镜像成功后才推进 `last_seq.cursor`。

因此即使出现断网、程序崩溃、服务器短暂不可用，也可以通过 cursor 继续恢复，不需要重复传输整个历史文件。

