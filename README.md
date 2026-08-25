# OpenDog 总流程说明

OpenDog 当前采用三端架构：

```text
PC A 采集端
  pc_a_agent/agent.py
  ├─ pc_a_agent/focused_app_history_ocr.py
  ├─ pc_a_agent/syncer.py
  └─ pc_a_agent/receiver.py
        |
        | HTTP POST /ingest
        | TCP 8899
        v
服务器
  FastAPI: server/app/main.py
  事件: JSONL + indexes.sqlite3
  消息: messages/JSONL + messages.sqlite3
        ^
        | HTTP GET /events
        | TCP 8899
        |
PC B 读取端
  pc_b_reader/reader.py
  pc_b_reader/sender.py
```

PC A 负责采集和接收弹窗消息，服务器负责持久化和中转，PC B 负责读取事件及发送消息。
PC A 和 PC B 不建立直接网络连接。

## 1. PC A 采集流程

PC A 内部包含三个程序：

```text
pc_a_agent/focused_app_history_ocr.py  负责采集
pc_a_agent/syncer.py                   负责上传
pc_a_agent/receiver.py                 负责接收并显示消息
```

平时只需通过 `pc_a_agent/agent.py` 一次启动并监管这三个程序。

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
python pc_a_agent/focused_app_history_ocr.py --no-save-images
```

## 2. PC A 上传流程

`pc_a_agent/syncer.py` 读取 `sync_config.json`，持续监控：

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

只有服务器确认成功后，`pc_a_agent/syncer.py` 才会更新 `offset.cursor`。如果上传失败，cursor 不推进，下次会从同一位置重试，避免丢数据。

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

单独运行上传器：

```powershell
python pc_a_agent/syncer.py --config sync_config.json
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
GET  /events/range  按时间范围查询事件
POST /messages      PC B 创建消息
GET  /messages/pull PC A 长轮询接收消息
POST /messages/ack  PC A 确认消息已经显示
```

服务器事件存储：

```text
/var/lib/opendog-ingest/indexes.sqlite3
/var/lib/opendog-ingest/timeline.jsonl
/var/lib/opendog-ingest/devices/<device_id>/events.jsonl
```

服务器消息存储：

```text
/var/lib/opendog-ingest/messages/messages.sqlite3
/var/lib/opendog-ingest/messages/timeline.jsonl
/var/lib/opendog-ingest/messages/targets/<device_id>/messages.jsonl
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
sudo sqlite3 /var/lib/opendog-ingest/indexes.sqlite3 \
'SELECT seq,event_id,source,device_id,event_type,event_ts FROM event_index ORDER BY seq DESC LIMIT 20;'
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

PC B 向 PC A 发送弹窗消息：

```powershell
python sender.py --config config.json --title "提醒" --body "消息内容"
```

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
PC B -> 服务器: POST /messages
PC A -> 服务器: GET /messages/pull、POST /messages/ack
PC A 和 PC B: 通过服务器间接传递消息
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
3. 在 PC A 运行 `python pc_a_agent\agent.py --config sync_config.json`
4. 确认服务器事件存储中出现记录
5. 在 PC B 运行 reader.py，确认生成 mirror_events.jsonl
6. 在 PC B 运行 sender.py 发送测试消息
7. 确认 PC A 弹出 MessageBoxW，并在服务器记录 ACK
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

整个链路遵守四个原则：

1. 本地先落盘，再上传。
2. 上传成功后才推进 PC A 的 `offset.cursor`。
3. PC B 写入本地镜像成功后才推进 `last_seq.cursor`。
4. PC A 显示并 ACK 消息后才推进 `message_seq.cursor`。

因此即使出现断网、程序崩溃、服务器短暂不可用，也可以通过 cursor 继续恢复，不需要重复传输整个历史文件。

