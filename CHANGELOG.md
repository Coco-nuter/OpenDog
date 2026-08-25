# Changelog

本文件记录 OpenDog 开发过程中的重要修改。项目当前仍处于开发阶段，所有记录均归入 `Unreleased`。

详细记录统一使用以下文件名格式：

```text
YYYY-MM-DD-<commit>-<tag>.md
```

## Unreleased

### 2026-08-25 · 50d5b5a · pc-messaging-prototype

- 将 PC A 的采集、同步和消息接收程序集中到 `pc_a_agent/`。
- 增加 PC B 文本消息发送程序。
- 增加服务端消息创建、长轮询拉取、ACK、持久化和设备权限隔离。
- 删除 Git 中旧的根目录 `app/`，保持 `android/` 不变。
- 将非主流程 Python 工具从 Git 移除，本地归档到 `history/python_tools/`。
- 11 项服务端及 PC A/PC B 消息链路测试通过。

详细说明：[PC A/PC B 消息传输原型](docs/changelog/2026-08-25-50d5b5a-pc-messaging-prototype.md)

对应实现提交：[`50d5b5a`](https://github.com/Coco-nuter/OpenDog/commit/50d5b5a)
