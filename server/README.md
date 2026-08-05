# OpenDog 服务器部署手册

该服务接收 PC A、手机或其他设备上传的事件，使用 Bearer Token 认证，并采用：

```text
JSONL 文件 = 完整事件主存储
SQLite     = 索引、去重、查询辅助
```

服务提供接口：

```text
GET  /health        健康检查，不需要 token
POST /ingest        接收事件，必须携带 Bearer Token
GET  /events        按全局 seq 增量读取事件，必须携带 Bearer Token
GET  /events/range  按事件时间范围读取所有设备事件，必须携带 Bearer Token
```

## 1. 存储结构

默认持久化目录：

```text
/var/lib/opendog-ingest/
├─ indexes.sqlite3
├─ timeline.jsonl
└─ devices/
   ├─ windows_pc_a/
   │  └─ events.jsonl
   └─ android_phone/
      └─ events.jsonl
```

各文件作用：

```text
devices/<device_id>/events.jsonl
```

保存某个设备的完整事件。

```text
timeline.jsonl
```

保存全局时间线索引，记录所有设备事件进入服务器的先后顺序和文件 offset。

```text
indexes.sqlite3
```

保存索引和去重信息，不再作为完整事件主存储。

## 2. 写入流程

客户端上传：

```text
POST /ingest
```

服务器处理流程：

```text
校验 token
        ↓
用 event_id 去重
        ↓
为新事件分配全局递增 seq
        ↓
写入 devices/<device_id>/events.jsonl
        ↓
写入 timeline.jsonl
        ↓
写入 SQLite 索引 event_index
        ↓
返回 count、duplicates、last_seq
```

如果客户端断线或重复上传同一条事件，服务器会根据 `event_id` 去重。

## 3. 读取流程

### 按 seq 增量读取

PC B 镜像同步推荐使用：

```http
GET /events?after_seq=10086&limit=500
Authorization: Bearer <token>
```

含义：

```text
读取 seq > 10086 的事件，最多 500 条
```

返回结果里包含：

```json
{
  "ok": true,
  "events": [],
  "last_seq": 10090,
  "has_more": false
}
```

PC B 应在本地写入成功后，再把 `last_seq.cursor` 更新为返回事件中的最后一个 `seq`。

### 按时间范围读取

查询某一段时间内所有设备事件：

```http
GET /events/range?start_ts=1785150000&end_ts=1785153600&limit=500
Authorization: Bearer <token>
```

排序方式：

```text
event_ts ASC, seq ASC
```

如果返回 `has_more=true`，继续带上 `next_cursor`：

```http
GET /events/range?start_ts=1785150000&end_ts=1785153600&limit=500&cursor=<next_cursor>
```

## 4. 运行要求

- Ubuntu 22.04 或 24.04
- Python 3.10 或更高
- 服务器磁盘上有持久化目录 `/var/lib/opendog-ingest`
- 客户端能够访问服务器 TCP 8899，或者通过 Nginx/HTTPS 访问
- 云服务器安全组需要开放对应端口

## 5. 上传代码到服务器

在 Windows 项目目录执行：

```powershell
scp -r .\server root@SERVER_IP:/tmp/opendog-server
```

登录服务器：

```bash
ssh root@SERVER_IP
```

如果 `/tmp/opendog-server` 下出现嵌套的 `server` 目录，实际代码可能在：

```text
/tmp/opendog-server/server
```

后续复制时按实际路径调整。

## 6. 安装系统依赖

```bash
sudo apt update
sudo apt install -y python3 python3-venv openssl sqlite3
python3 --version
```

Python 版本应为 3.10 或更高。

## 7. 安装服务文件

创建系统用户和目录：

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin opendog 2>/dev/null || true
sudo mkdir -p /opt/opendog-server /var/lib/opendog-ingest
sudo cp -a /tmp/opendog-server/. /opt/opendog-server/
sudo chown -R root:root /opt/opendog-server
sudo chown -R opendog:opendog /var/lib/opendog-ingest
```

创建虚拟环境并安装依赖：

```bash
sudo python3 -m venv /opt/opendog-server/.venv
sudo /opt/opendog-server/.venv/bin/python -m pip install --upgrade pip
sudo /opt/opendog-server/.venv/bin/pip install -r /opt/opendog-server/requirements.txt
```

## 8. 配置环境变量

安装环境变量文件：

```bash
sudo install -m 600 -o root -g root \
  /opt/opendog-server/deploy/opendog-ingest.env.example \
  /etc/opendog-ingest.env
sudo nano /etc/opendog-ingest.env
```

推荐配置：

```text
OPENDOG_TOKEN=replace-with-a-long-random-token
OPENDOG_DATA_DIR=/var/lib/opendog-ingest
OPENDOG_DATABASE_PATH=/var/lib/opendog-ingest/indexes.sqlite3
OPENDOG_MAX_BATCH_SIZE=100
OPENDOG_MAX_BODY_BYTES=2097152
OPENDOG_HOST=0.0.0.0
OPENDOG_PORT=8899
```

生成 token：

```bash
openssl rand -hex 32
```

同一个 token 必须同时填写到：

```text
服务器 /etc/opendog-ingest.env 的 OPENDOG_TOKEN
客户端 sync_config.json 或 pc_b_reader/config.json 的 token
```

不要在 token 两侧加引号或空格。

## 9. 安装并启动 systemd 服务

```bash
sudo install -m 644 \
  /opt/opendog-server/deploy/opendog-ingest.service \
  /etc/systemd/system/opendog-ingest.service
sudo systemctl daemon-reload
sudo systemctl enable --now opendog-ingest
```

检查状态：

```bash
sudo systemctl status opendog-ingest --no-pager
sudo journalctl -u opendog-ingest -n 100 --no-pager
```

本机健康检查：

```bash
curl http://127.0.0.1:8899/health
```

预期响应：

```json
{"ok":true,"service":"opendog-ingest","storage":"jsonl-with-sqlite-index"}
```

## 10. 网络访问

临时联调可以开放 TCP 8899：

```bash
sudo ufw allow OpenSSH
sudo ufw allow from CLIENT_PUBLIC_IP to any port 8899 proto tcp
sudo ufw enable
sudo ufw status
```

云服务器还需要在安全组里放行 TCP 8899。

正式部署建议使用 Nginx + HTTPS，并让应用只监听本机：

```text
OPENDOG_HOST=127.0.0.1
OPENDOG_PORT=8899
```

复制 Nginx 配置：

```bash
sudo cp /opt/opendog-server/deploy/nginx-opendog.conf \
  /etc/nginx/sites-available/opendog-ingest
sudo sed -i 's/ingest.example.com/YOUR_DOMAIN/g' \
  /etc/nginx/sites-available/opendog-ingest
sudo ln -sfn /etc/nginx/sites-available/opendog-ingest \
  /etc/nginx/sites-enabled/opendog-ingest
sudo nginx -t
sudo systemctl reload nginx
```

## 11. 手动验证写入

在服务器上设置 token 变量：

```bash
TOKEN='这里填写服务器 token'
```

发送测试事件：

```bash
curl -i http://127.0.0.1:8899/ingest \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{
    "source":"manual_test",
    "device_id":"server_test",
    "events":[{
      "event_id":"manual-test-001",
      "type":"focused_window_ocr",
      "ts":1782355279.482,
      "data":{"app":"chrome.exe","title":"test","text":"hello"}
    }]
  }'
```

预期响应：

```json
{"ok":true,"count":1,"duplicates":0,"last_seq":1}
```

重复发送同一个 `event_id`，预期：

```json
{"ok":true,"count":0,"duplicates":1,"last_seq":1}
```

## 12. 手动验证读取

按 seq 读取：

```bash
curl -H "Authorization: Bearer ${TOKEN}" \
"http://127.0.0.1:8899/events?after_seq=0&limit=5"
```

按时间范围读取：

```bash
curl -H "Authorization: Bearer ${TOKEN}" \
"http://127.0.0.1:8899/events/range?start_ts=1782350000&end_ts=1782360000&limit=5"
```

查看 JSONL 文件：

```bash
sudo tail -n 5 /var/lib/opendog-ingest/timeline.jsonl
sudo find /var/lib/opendog-ingest/devices -name events.jsonl -print
sudo tail -n 5 /var/lib/opendog-ingest/devices/server_test/events.jsonl
```

查看 SQLite 索引：

```bash
sudo sqlite3 /var/lib/opendog-ingest/indexes.sqlite3 \
'SELECT seq,event_id,source,device_id,event_type,event_ts,device_path FROM event_index ORDER BY seq DESC LIMIT 10;'
```

## 13. 当前版本数据兼容

当前版本只使用新格式数据：

```text
/var/lib/opendog-ingest/indexes.sqlite3
/var/lib/opendog-ingest/timeline.jsonl
/var/lib/opendog-ingest/devices/<device_id>/events.jsonl
```

服务启动时会创建缺失的目录、`timeline.jsonl` 和 SQLite 索引表，但不会迁移旧版
`events.sqlite3` 里的数据。

如果服务器上已经有当前版本生成的数据，保留这些文件即可：

```text
indexes.sqlite3
timeline.jsonl
devices/
```

如果不需要旧版 SQLite 数据，可以删除：

```bash
sudo systemctl stop opendog-ingest
sudo rm -f /var/lib/opendog-ingest/events.sqlite3*
sudo systemctl start opendog-ingest
```

如果之前迁移失败留下了半成品文件，并且你不需要这些数据，可以全新清空：

```bash
sudo systemctl stop opendog-ingest
sudo rm -f /var/lib/opendog-ingest/events.sqlite3*
sudo rm -f /var/lib/opendog-ingest/indexes.sqlite3*
sudo rm -f /var/lib/opendog-ingest/timeline.jsonl
sudo rm -rf /var/lib/opendog-ingest/devices
sudo mkdir -p /var/lib/opendog-ingest/devices
sudo chown -R opendog:opendog /var/lib/opendog-ingest
sudo systemctl start opendog-ingest
```

## 14. 更新服务

在 Windows 上传新代码：

```powershell
scp -r .\server root@SERVER_IP:/tmp/opendog-server
```

在服务器执行：

```bash
sudo cp -a /tmp/opendog-server/. /opt/opendog-server/
sudo chown -R root:root /opt/opendog-server
sudo /opt/opendog-server/.venv/bin/pip install \
  -r /opt/opendog-server/requirements.txt
sudo systemctl daemon-reload
sudo systemctl restart opendog-ingest
sudo systemctl status opendog-ingest --no-pager
```

如果只是替换 `main.py`：

```bash
sudo install -m 644 -o root -g root \
  /tmp/opendog-server/app/main.py \
  /opt/opendog-server/app/main.py
sudo systemctl restart opendog-ingest
```

## 15. 日常维护

查看实时日志：

```bash
sudo journalctl -u opendog-ingest -f
```

检查数据目录大小：

```bash
sudo du -h /var/lib/opendog-ingest
```

备份索引数据库：

```bash
sudo mkdir -p /var/backups/opendog-ingest
sudo sqlite3 /var/lib/opendog-ingest/indexes.sqlite3 \
  ".backup '/var/backups/opendog-ingest/indexes-$(date +%F-%H%M%S).sqlite3'"
```

备份 JSONL 主数据：

```bash
sudo tar -czf "/var/backups/opendog-ingest/jsonl-$(date +%F-%H%M%S).tar.gz" \
  -C /var/lib/opendog-ingest timeline.jsonl devices
```
