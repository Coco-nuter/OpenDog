# PC A/PC B 消息传输原型

| 项目 | 内容 |
| --- | --- |
| 日期 | 2026-08-25 |
| 状态 | 开发中（Unreleased） |
| 实现提交 | [`50d5b5a`](https://github.com/Coco-nuter/OpenDog/commit/50d5b5a) |
| 记录标签 | `pc-messaging-prototype` |

## 修改目的

在现有 PC A 桌面采集和服务端同步能力之上，建立 PC B 到 PC A 的基础消息链路：PC B 通过服务器发送文本消息，PC A 长轮询接收消息并显示 Windows 基础弹窗，同时通过 ACK 记录投递完成状态。

本次修改仍属于开发阶段原型，不代表正式发布版本。

## 主要修改

### PC A 代码整合

PC A 的运行代码集中到 `pc_a_agent/`：

```text
pc_a_agent/
├─ agent.py
├─ focused_app_history_ocr.py
├─ syncer.py
└─ receiver.py
```

- `agent.py` 使用一条命令启动并监管采集、上传和消息接收三个子进程。
- `focused_app_history_ocr.py` 负责前台窗口采集、屏幕变化检测和 OCR 事件生成。
- `syncer.py` 增量读取 `focus_history/history.jsonl` 并上传到服务端。
- `receiver.py` 长轮询消息，先写入本地收件记录，再调用 Windows `MessageBoxW` 显示弹窗，最后发送 ACK 并推进游标。
- 子进程工作目录继续使用项目根目录，因此 `sync_config.json`、`focus_history/` 和 `sync_state/` 的路径保持兼容。

### PC B 消息发送

- 新增 `pc_b_reader/sender.py`。
- 支持消息标题、正文、目标设备、附加 JSON 数据和过期时间。
- 每条消息使用稳定的 `message_id`，网络重试时服务端不会重复保存同一条消息。
- PC B 使用独立消息 Token，不能读取或确认 PC A 的消息。

### 服务端消息接口

新增以下接口：

```text
POST /messages
GET  /messages/pull
POST /messages/ack
```

- `POST /messages`：PC B 创建发送给指定设备的消息。
- `GET /messages/pull`：PC A 使用长轮询增量拉取消息。
- `POST /messages/ack`：PC A 确认消息已经显示。
- 服务端校验 PC A、PC B 的独立 Token 和目标设备权限。
- 消息使用 SQLite 保存索引、投递和 ACK 状态，同时使用 JSONL 保存时间线及目标设备完整记录。
- 按 `message_id` 去重，避免客户端重试造成重复消息。

### 部署和配置

- 更新服务器环境变量模板，增加 PC A/PC B 消息 Token 和设备 ID。
- 更新 Nginx 配置以支持长轮询接口。
- 更新 `sync_config.example.json` 和 `pc_b_reader/config.example.json`。
- 增加 `requirements-desktop.txt`，记录当前 PC A 采集运行所需的直接依赖。
- 更新根目录、PC A、PC B 和服务器说明文档。

### 目录整理

- 从 Git 删除旧的根目录 `app/`。
- 已存在的 `android/` 项目保持不变。
- 本地 `app_plan/` 整体加入忽略规则，不上传 GitHub。
- 根目录不再保留 Python 入口，PC A 运行脚本统一放在 `pc_a_agent/`。
- 6 个不参与当前主流程的 Python 工具从 Git 移除，本地分类保存在 `history/python_tools/`。

本地归档包括：

```text
history/python_tools/
├─ activitywatch_monitor/aw_monitor.py
├─ diagnostics/test_uia.py
├─ image_comparison/compare_pic.py
├─ image_transfer/upload_images.py
└─ legacy_collectors/
   ├─ realtime_window.py
   └─ screen_change_ocr.py
```

## 云端与本地边界

以下内容已提交到 GitHub：

- PC A、PC B 和服务端源代码。
- 自动化测试。
- 脱敏配置模板。
- 部署模板、依赖清单和说明文档。

以下内容仅保存在本地，没有提交：

- `app_plan/`
- `history/`
- `.venv/`
- `sync_config.json`
- `pc_b_reader/config.json`
- Token 和其他真实凭据
- `focus_history/` 采集数据
- `sync_state/` 游标、收件箱和日志
- Python、Gradle 和 Android 构建缓存

## 当前启动方式

在项目根目录启动 PC A：

```powershell
python pc_a_agent\agent.py --config sync_config.json
```

PC B 发送基础弹窗消息：

```powershell
python pc_b_reader\sender.py `
  --config pc_b_reader\config.json `
  --title "OpenDog" `
  --body "Message from PC B"
```

## 验证结果

- 11 项服务端及 PC A/PC B 消息链路测试全部通过。
- PC A 采集、同步、Agent 和消息接收模块导入通过。
- Agent 构造的采集、上传和接收三个子进程路径验证通过。
- PC B 发送、PC A 拉取、显示后 ACK 的 HTTP 集成链路通过。
- Token、目标设备和跨设备访问限制测试通过。
- `android/` 相对上一远端提交的修改数量为 0。
- Git 工作区在功能提交和推送后保持干净。

## 当前限制与后续工作

- PC A 当前使用阻塞式 Windows `MessageBoxW`，尚未实现通知中心或非阻塞弹窗。
- 尚未完成长时间后台运行、断网恢复和多次真实人工交互验收。
- PC B 当前主要通过命令行发送消息，尚未完成完整图形界面。
- 当前仅支持 `popup_text` 消息类型，尚未支持文件、图片或富文本消息。
- 项目仍处于开发阶段，因此本记录保留在 `Unreleased`，暂不创建正式 Release。
