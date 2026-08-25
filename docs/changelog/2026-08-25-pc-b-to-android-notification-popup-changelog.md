# PC B sends messages to Android and displays notification popups

`2026-08-25 · pc-b-to-android-notification-popup · changelog`

## Android 端

- 使用独立 Message Token 和 Room 消息数据库。
- 通过 `/messages/pull` 长轮询接收消息，通过 `/messages/ack` 确认展示结果。
- 按 `message_id`、`msg_seq` 去重；ACK 失败保留状态且不重复通知。
- 使用前台服务、网络指数退避，以及 LOW 常驻渠道和 HIGH 消息通知渠道。
- 支持 Android 13+ 通知权限、通知禁用检测、设置页启停和测试通知。
- Message Token 和消息数据库不参与系统备份。

## 服务器与 PC 端

- 服务器使用“设备 ID → 独立 Token”配置，同时注册 PC A 和当前 Android 设备。
- PC B 可以向指定设备发送 `popup_text`，未知设备和跨设备访问返回 HTTP 403。
- 删除旧 PC A Token 回退兼容，PC A/PC B 均强制使用独立 `message_token`。
- 旧 Android 设备 ID 未保留，Android 工程协议无需修改即可完成对接。

## 验证

- Android：19/19 JVM 测试通过，构建成功，Lint 0 Error。
- 服务器和 PC 客户端：13/13 自动测试通过。
- 在线验证：PC B → PC A、PC B → Android 的发送、拉取和 ACK 均成功。
- 隔离验证：Android Token 读取 PC A 消息、PC B 向旧 Android ID 发送消息均返回 HTTP 403。

手机端仍需填写新 Android Token，完成真实通知、后台、锁屏及荣耀省电模式测试。

## Token 位置

- 本地安全副本：`history/local_configs/opendog-message-receivers.json`（不会上传 Git）。
- PC A：`sync_config.json` 的 `message_token`。
- 服务器：`/etc/opendog-message-receivers.json`。

Tag：`dev-2026-08-25-pc-b-to-android-notification-popup`
