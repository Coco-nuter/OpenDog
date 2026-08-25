# PC A Agent

PC A Agent 用一条命令启动并监管现有采集程序、上传程序和消息接收程序。
收到的 `popup_text` 消息会先保存到本地，再通过 Windows `MessageBoxW` 显示。

在项目根目录运行：

```powershell
python pc_a_agent\agent.py --config sync_config.json
```

默认不保存截图。如需保存截图：

```powershell
python pc_a_agent\agent.py --config sync_config.json --save-images
```

消息运行文件保存在 `sync_config.json` 配置的 `state_dir`：

```text
message_seq.cursor
message_inbox.jsonl
receiver.log
```

接收器优先使用 `sync_config.json` 中的 `message_token`。没有配置时会回退使用
原来的 `token`，以兼容只配置 `OPENDOG_TOKEN` 的服务器。

运行 Agent 前，应先停止手动启动的 `pc_a_agent/focused_app_history_ocr.py` 和
`pc_a_agent/syncer.py`，
避免两个上传程序同时操作同一个 cursor。按 `Ctrl+C` 会停止 Agent 及其三个子进程。
