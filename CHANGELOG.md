# Changelog

本文件记录 OpenDog 开发过程中的重要修改。项目当前仍处于开发阶段，所有记录均归入 `Unreleased`。

新的详细记录统一使用以下文件名格式：

```text
YYYY-MM-DD-<feature-tag>-changelog.md
```

## Unreleased

### 2026-08-25 · pc-b-to-android-notification-popup

- Android 端完成独立 Message Token、Room 去重、长轮询/ACK、前台服务及高优先级通知弹窗。
- 服务端启用多设备独立 Token，PC B 可以分别向 PC A 和 Android 发送消息。
- 删除旧 PC A Token 兼容和旧 Android 设备，跨设备访问返回 HTTP 403。
- Android 19/19 测试、服务端 13/13 测试及在线消息链路验证通过。

详细说明：[PC B 向 Android 发送消息并弹出通知](docs/changelog/2026-08-25-pc-b-to-android-notification-popup-changelog.md)

### 2026-08-25 · 50d5b5a · pc-messaging-prototype

- 将 PC A 的采集、同步和消息接收程序集中到 `pc_a_agent/`。
- 增加 PC B 文本消息发送程序。
- 增加服务端消息创建、长轮询拉取、ACK、持久化和设备权限隔离。
- 删除 Git 中旧的根目录 `app/`，保持 `android/` 不变。
- 将非主流程 Python 工具从 Git 移除，本地归档到 `history/python_tools/`。
- 11 项服务端及 PC A/PC B 消息链路测试通过。

详细说明：[PC A/PC B 消息传输原型](docs/changelog/2026-08-25-50d5b5a-pc-messaging-prototype.md)

对应实现提交：[`50d5b5a`](https://github.com/Coco-nuter/OpenDog/commit/50d5b5a)
