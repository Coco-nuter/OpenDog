# 桌面端采集与同步实现说明

本文档整理 `focused_app_history_ocr.py` 和 `syncer.py` 的桌面端实现方式，供手机版实现相同功能时对齐数据模型、触发逻辑和同步协议。

## 1. 总体架构

桌面端分成两个独立进程：

1. `focused_app_history_ocr.py`
   - 负责监听用户输入、识别当前前台窗口、截屏、OCR、生成事件。
   - 将事件追加写入本地 `focus_history/history.jsonl`。
   - 可选保存截图 PNG，但当前普通历史事件里不包含图片路径，也不会由 `syncer.py` 上传图片。

2. `syncer.py`
   - 负责持续读取 `history.jsonl` 新增的完整行。
   - 将本地事件转换成服务端 `/ingest` 需要的 envelope 格式。
   - 使用字节 offset 游标做断点续传。
   - 成功上传后才推进游标。

所以手机版可以选择两种实现路径：

- 路径 A：复刻桌面端两段式流程，先写本地 JSONL，再做增量同步。
- 路径 B：直接在手机端采集后调用 `/ingest`，但事件字段和 envelope 结构仍需与 `syncer.py` 输出保持兼容。

## 2. 桌面端采集方式

### 2.1 使用的系统能力

桌面端是 Windows 专用实现，依赖这些能力：

- `uiautomation`：读取当前焦点控件，并向上找到顶层窗口。
- Win32 API：
  - `GetForegroundWindow()` 获取当前前台窗口句柄。
  - `GetWindowThreadProcessId()` 获取窗口所属进程 ID。
  - `OpenProcess()` 和 `QueryFullProcessImageNameW()` 获取进程可执行文件名。
  - `GetWindowRect()` 作为窗口区域兜底。
- `dxcam`：按窗口区域截屏，输出 BGR 图像。
- `pynput`：监听键盘按下、鼠标点击、鼠标滚轮。
- `RapidOCR`：对截图或差异区域做 OCR。
- `OpenCV`：比较同一窗口前后截图，提取变化区域。

### 2.2 初始化流程

`FocusedAppHistoryOCR.initialize()` 做以下事情：

1. 创建输出目录，默认 `focus_history/`。
2. 初始化 `dxcam.create(output_idx=0, output_color="BGR")`。
3. 如果未禁用 OCR，则初始化 `RapidOCRTextRecognizer`。
4. 启动键盘和鼠标监听器。

默认命令行参数中比较关键的是：

- `--poll-interval`：主循环轮询间隔，默认 `0.3` 秒。
- `--idle-seconds`：用户输入后等待多久认为交互结束，默认 `2.0` 秒。
- `--focus-switch-settle-seconds`：焦点切换后等待再确认一次，默认 `0.2` 秒。
- `--output-dir`：事件和截图目录，默认 `focus_history`。
- `--source`：事件来源，默认 `pc_a`。
- `--exclude-app`：跳过指定进程名，可多次传入。

## 3. 采集触发逻辑

桌面端不是固定频率截图，而是由“焦点变化”和“用户交互后空闲”触发。

### 3.1 主循环

`loop()` 无限执行：

1. 调用 `process_once()`。
2. 捕获单次循环异常并计数。
3. sleep `poll_interval`。

`process_once()` 是核心状态机。

### 3.2 获取当前前台窗口

`get_focused_window()` 的逻辑：

1. `auto.GetFocusedControl()` 获取焦点控件。
2. `control.GetTopLevelControl()` 找到顶层窗口。
3. 优先读取顶层窗口的 `NativeWindowHandle`，没有则用 `GetForegroundWindow()`。
4. 根据窗口句柄查进程名，例如 `chrome.exe`。
5. 读取窗口标题 `window.Name`。
6. 生成 `focus_id = "{app}:{hwnd}"`。
7. 使用 `window.BoundingRectangle` 作为截图区域。
8. 如果 UIAutomation 区域不可用或太小，则用 Win32 `GetWindowRect()` 兜底。
9. 区域会被裁剪到屏幕范围内，并要求最小宽高：
   - 默认最小宽度 `200`
   - 默认最小高度 `120`

返回结构：

```json
{
  "focus_id": "chrome.exe:123456",
  "app": "chrome.exe",
  "title": "页面标题",
  "hwnd": 123456,
  "region": [left, top, right, bottom]
}
```

### 3.3 焦点切换触发

当本轮 `focus_id != current_focus_id` 时：

1. 如果配置了 `focus_switch_settle_seconds`，先等待默认 `0.2` 秒。
2. 再读取一次当前焦点窗口。
3. 只有两次读取到的 `focus_id` 相同，才认为焦点稳定。
4. 如果目标 app 在排除列表中，则跳过。
5. 为新的 `focus_id` 初始化内存状态。
6. 触发一次截图和 OCR，事件 `trigger = "focus_switch"`。

首次进入某个窗口时没有上一帧，因此保存完整窗口截图并对完整窗口做 OCR。

### 3.4 用户交互后空闲触发

键盘、鼠标点击、鼠标滚轮监听器都会调用 `mark_activity()`：

1. 读取当前 `GetForegroundWindow()`。
2. 将该窗口句柄保存为 `pending_activity_hwnd`。
3. 记录 `last_activity_at = time.monotonic()`。

主循环中，如果当前窗口句柄等于 `pending_activity_hwnd`，并且距离最后一次输入已经超过 `idle_seconds`，则：

1. 清空 `pending_activity_hwnd`，避免重复触发。
2. 截图并 OCR。
3. 事件 `trigger = "interaction_idle"`。

这个设计的含义是：用户连续输入、点击或滚动期间不会每次都 OCR，而是在停止操作一段时间后采一次结果。

## 4. 截图、差异裁剪和 OCR

### 4.1 截图

`capture_region(region)` 直接调用：

```python
frame = self.camera.grab(region=region)
```

截图区域是当前窗口区域，图像格式是 OpenCV 常用的 BGR ndarray。

### 4.2 同窗口截图差异比较

默认开启 `compare_screenshots=True`。

当同一个 `focus_id` 已经有上一帧 `last_frame` 时，`save_event()` 会先比较上一帧和当前帧：

1. 如果两帧尺寸不同，将旧图 resize 到新图尺寸。
2. 转灰度。
3. 计算绝对差 `cv2.absdiff()`。
4. 阈值化：差异像素阈值固定为 `80`。
5. 使用 `9x9` 矩形 kernel 膨胀 `5` 次，把临近变化合并。
6. 查找外部轮廓。
7. 丢弃面积小于 `diff_min_area` 的轮廓，默认 `100`。
8. 每个保留轮廓计算 bounding box，并外扩 `diff_margin`，默认 `8` 像素。
9. 按面积降序取前 3 个变化区域。
10. 将这最多 3 个区域合并成一个总 bounding box。

如果没有有效差异，直接跳过，不生成事件。

### 4.3 差异区域过滤

比较同窗口截图时，必须满足：

- 合并后的差异裁剪宽度 >= `min_diff_width`，默认 `100`。
- 合并后的差异裁剪高度 >= `min_diff_height`，默认 `80`。
- 所有有效差异区域总面积占窗口面积比例 >= `diff_min_ratio`，默认 `0.2`。

不满足则跳过事件。

注意：`diff_min_ratio=0.2` 比较高，表示同窗口变化面积至少达到窗口 20% 才记录。手机端如果要捕捉较小 UI 变化，可以调低对应阈值。

### 4.4 OCR 输入图像

OCR 输入取决于场景：

- 新窗口第一次采集：完整窗口截图。
- 同窗口后续采集且差异过滤通过：只对变化裁剪区域 OCR。
- 关闭 `--no-compare-screenshots` 后：每次都对完整窗口 OCR。

### 4.5 OCR 实现

`RapidOCRTextRecognizer` 的默认配置：

- `min_score = 0.5`
- `use_det = True`
- `use_cls = False`
- `use_rec = True`
- `limit_side_len = 960`

OCR 前会按最长边缩放：

- 如果图像最长边大于 `960`，等比例缩小到最长边 `960`。
- 这样减少 OCR 计算量。

OCR 输出会过滤低置信度文本，然后只把文本按行拼接：

```text
第一行文字
第二行文字
第三行文字
```

桌面端普通事件不会保存 OCR box 和 score，只保存最终拼接后的 `text`。

## 5. 本地状态和文件结构

### 5.1 内存状态

每个 `focus_id` 在 `focus_library` 中有一份状态：

```python
{
  "app": "...",
  "title": "...",
  "hwnd": 123456,
  "region": (left, top, right, bottom),
  "last_text": "",
  "last_frame": None,
  "last_image_path": None,
  "last_seen_at": 0,
  "history": deque(maxlen=max_memory_history)
}
```

其中：

- `last_frame` 用于下一次同窗口差异比较。
- `history` 只保留内存最近事件，默认每个 focus 最多 `100` 条。
- 程序重启后内存状态清空，但 `history.jsonl` 保留。

### 5.2 输出目录

默认输出目录：

```text
focus_history/
  history.jsonl
  chrome.exe_chrome.exe_123456/
    history.jsonl
    20260708_120000_123456_focus_switch.png
    debug_captures/
    debug_captures.jsonl
```

每条事件会同时追加到：

- 全局：`focus_history/history.jsonl`
- 当前窗口目录：`focus_history/{safe_app}_{safe_focus_id}/history.jsonl`

其中目录名会替换 Windows 文件名非法字符。

### 5.3 图片保存策略

默认保存 PNG：

- 新窗口第一次保存完整窗口截图。
- 同窗口后续保存差异裁剪图。

但普通 `history.jsonl` 事件里没有 `image_path` 字段。图片主要用于本地调试或后续人工检查。

默认每个 focus 目录最多保留 `500` 张普通 PNG。超过后删除最旧图片。

如果开启 `--debug-all-captures`：

- 保存每次触发的完整调试截图。
- 保存差异比较的 previous/current/annotated 三张图。
- 写入 `debug_captures.jsonl`。

## 6. 本地事件格式

`focused_app_history_ocr.py` 写入 JSONL 的事件格式如下：

```json
{
  "event_id": "uuid",
  "source": "pc_a",
  "type": "focused_window_ocr",
  "timestamp": "2026-07-08 12:00:00.123",
  "trigger": "focus_switch",
  "focus_id": "chrome.exe:123456",
  "app": "chrome.exe",
  "title": "页面标题",
  "text": "OCR 文本"
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `event_id` | UUID，服务端用它去重 |
| `source` | 逻辑来源，默认 `pc_a` |
| `type` | 固定为 `focused_window_ocr` |
| `timestamp` | 本地时间字符串，精确到毫秒，没有时区 |
| `trigger` | `focus_switch` 或 `interaction_idle` |
| `focus_id` | `{app}:{hwnd}` |
| `app` | 进程可执行文件名 |
| `title` | 窗口标题 |
| `text` | OCR 后的纯文本 |

手机版建议保留同样语义，但字段值可以按移动端场景替换：

- `app`：包名或应用显示名，例如 `com.tencent.mm`。
- `title`：当前 Activity、页面标题、无障碍节点标题或空字符串。
- `focus_id`：建议使用稳定页面标识，例如 `{package}:{activity}`，不要使用每次变化的对象地址。
- `trigger`：可继续使用 `focus_switch` 和 `interaction_idle`；如果有手机特有触发，也可以新增，但服务端当前不会限制。
- `source`：例如 `android_a` 或 `ios_a`。

## 7. 同步器配置

`syncer.py` 默认读取 `sync_config.json`。示例：

```json
{
  "server_url": "http://SERVER_IP:8899",
  "token": "replace-with-the-server-token",
  "source": "pc_a",
  "device_id": "windows_pc_a",
  "history_file": "focus_history/history.jsonl",
  "state_dir": "sync_state",
  "batch_size": 20,
  "flush_interval_seconds": 1,
  "request_timeout_seconds": 15,
  "max_retry_seconds": 60,
  "use_proxy": false
}
```

必填项：

- `server_url`
- `token`
- `source`
- `device_id`
- `history_file`

默认项：

- `state_dir = sync_state`
- `batch_size = 20`
- `flush_interval_seconds = 1.0`
- `request_timeout_seconds = 15.0`
- `max_retry_seconds = 60.0`
- `use_proxy = false`

## 8. 增量读取 JSONL 的逻辑

### 8.1 字节游标

同步器使用 `sync_state/offset.cursor` 保存已确认上传的字节偏移：

```json
{
  "path": "绝对路径/focus_history/history.jsonl",
  "offset": 12345,
  "updated_at": "2026-07-08T12:00:00+08:00"
}
```

启动时：

1. 如果没有 cursor，从 `0` 开始。
2. 如果 cursor 记录的 path 和当前 history 文件不同，视为致命错误并退出。
3. 如果 history 文件大小小于 cursor offset，说明文件被截断，重置 offset 到 `0`。

### 8.2 只读取完整行

`read_complete_lines()` 从 offset 开始按行读取：

1. 一次最多读取 `batch_size` 行。
2. 只处理以 `\n` 结尾的完整行。
3. 如果读到最后一行没有换行，认为写入尚未完成，暂不处理。
4. 每条记录保存：
   - 起始字节
   - 结束字节
   - 原始文本
   - JSON 解析结果
   - 解析错误

如果第一次读到的记录数少于 `batch_size`，会等待 `flush_interval_seconds` 后再读一次，用来聚合短时间内新增的事件。

### 8.3 坏数据处理

同步器不会因为某一行坏 JSON 永久卡住：

- JSON 解析失败的行会进入 `dead_letter.jsonl`。
- 能正常解析的事件继续上传。
- 上传或处理完成后，cursor 推进到本批次末尾。

`dead_letter.jsonl` 记录：

```json
{
  "recorded_at": "2026-07-08T12:00:00+08:00",
  "byte_start": 100,
  "byte_end": 200,
  "error": "错误信息",
  "raw_line": "原始 JSONL 行"
}
```

## 9. 事件归一化逻辑

`normalize_event()` 将本地 JSONL 事件转成服务端事件：

```json
{
  "event_id": "uuid",
  "type": "focused_window_ocr",
  "ts": 1783502400.123,
  "data": {
    "timestamp": "2026-07-08 12:00:00.123",
    "trigger": "focus_switch",
    "focus_id": "chrome.exe:123456",
    "app": "chrome.exe",
    "title": "页面标题",
    "text": "OCR 文本"
  }
}
```

规则：

1. `source`
   - 本地事件有 `source` 就用事件里的。
   - 没有则用配置里的 `source`。

2. `device_id`
   - 本地事件有 `device_id` 就用事件里的。
   - 没有则用配置里的 `device_id`。

3. `event_id`
   - 本地事件有 `event_id` 就直接使用。
   - 没有则根据 `device_id + raw_line` 计算 SHA-256，生成 `legacy_{digest}`。

4. `type`
   - 本地事件有 `type` 就使用。
   - 没有则默认 `focused_window_ocr`。

5. `ts`
   - 如果本地事件已有 `ts`，直接使用。
   - 否则解析 `timestamp`，格式必须是 `%Y-%m-%d %H:%M:%S.%f`。
   - `datetime.timestamp()` 会按运行同步器机器的本地时区解释这个无时区时间。

6. `data`
   - 把 envelope 字段排除后，剩余字段全部放进 `data`。
   - 被排除的字段：`event_id`, `source`, `device_id`, `type`, `ts`。

手机版如果直接调用服务端，建议直接生成 `ts` 浮点 Unix 时间戳，避免无时区字符串在不同设备上被错误解释。

## 10. 上传协议

同步器向服务端发送：

```http
POST /ingest
Authorization: Bearer <token>
Content-Type: application/json; charset=utf-8
Accept: application/json
```

请求体：

```json
{
  "source": "pc_a",
  "device_id": "windows_pc_a",
  "events": [
    {
      "event_id": "uuid",
      "type": "focused_window_ocr",
      "ts": 1783502400.123,
      "data": {
        "timestamp": "2026-07-08 12:00:00.123",
        "trigger": "focus_switch",
        "focus_id": "chrome.exe:123456",
        "app": "chrome.exe",
        "title": "页面标题",
        "text": "OCR 文本"
      }
    }
  ]
}
```

服务端成功响应：

```json
{
  "ok": true,
  "count": 1,
  "duplicates": 0,
  "last_seq": 123
}
```

服务端根据 `event_id` 去重。重复上传同一事件不会产生重复数据。

## 11. 上传错误处理

`syncer.py` 的错误策略：

| 场景 | 处理 |
| --- | --- |
| HTTP `400` / `422` | 认为批次永久无效，整批写入 dead letter，推进 cursor |
| HTTP `409` | 当作成功，兼容重复批次 |
| HTTP `401` / `403` | 认证失败，致命错误，退出 |
| 其他 HTTP 错误 | 可重试错误 |
| 网络失败 / 超时 | 可重试错误 |
| 服务端返回非 JSON | 可重试错误 |
| 服务端 JSON 中 `ok` 不是 `true` | 可重试错误 |

可重试错误使用指数退避：

- 初始 `3` 秒。
- 每次翻倍。
- 最大不超过 `max_retry_seconds`，默认 `60` 秒。

只有上传成功、永久拒绝并写入 dead letter、或无有效事件但批次可前进时，才保存 cursor。

## 12. 手机端实现建议

### 12.1 Android 对应思路

可用能力通常包括：

- 前台应用识别：
  - `UsageStatsManager` 查询前台包名，或
  - `AccessibilityService` 监听窗口变化事件。
- 页面/控件文本：
  - 优先使用 `AccessibilityService` 读取节点文本、contentDescription、窗口标题。
  - 必要时使用 `MediaProjection` 截屏 + OCR。
- 用户交互触发：
  - 无障碍事件中的点击、滚动、文本变化、窗口内容变化。
  - 或在输入法/辅助服务内记录交互时间。
- 截图：
  - `MediaProjection`，但需要用户授权。

推荐手机端优先用无障碍文本树生成 `text`，只在节点文本不足时再 OCR。这样比全屏截图 OCR 更省电，也更稳定。

### 12.2 iOS 对应限制

iOS 普通 App 很难长期后台读取其他 App 的前台窗口和截图。除非是受管设备、特定企业权限或越狱环境，否则无法完整复刻 Windows 端能力。

如果目标是 iOS，建议先确认系统权限边界，再决定是否只能采集本 App 内内容。

### 12.3 手机端事件字段建议

直接上传服务端时，建议事件结构：

```json
{
  "source": "android_a",
  "device_id": "android_pixel_8",
  "events": [
    {
      "event_id": "uuid",
      "type": "focused_window_ocr",
      "ts": 1783502400.123,
      "data": {
        "timestamp": "2026-07-08 12:00:00.123",
        "trigger": "focus_switch",
        "focus_id": "com.example.app/.MainActivity",
        "app": "com.example.app",
        "title": "页面标题",
        "text": "采集到的文本"
      }
    }
  ]
}
```

如果仍采用 JSONL + 同步器模式，本地 JSONL 可以写成：

```json
{"event_id":"uuid","source":"android_a","device_id":"android_pixel_8","type":"focused_window_ocr","ts":1783502400.123,"timestamp":"2026-07-08 12:00:00.123","trigger":"focus_switch","focus_id":"com.example.app/.MainActivity","app":"com.example.app","title":"页面标题","text":"采集到的文本"}
```

包含 `ts` 可以避免同步器解析无时区 `timestamp`。

### 12.4 触发逻辑对齐建议

手机端可以复刻桌面端状态机：

1. 维护 `current_focus_id`。
2. 监听窗口/前台应用变化。
3. 发生变化后延迟一个短时间，例如 `200ms`，再次确认页面稳定。
4. 稳定后触发 `focus_switch` 事件。
5. 监听点击、滚动、输入、窗口内容变化等交互事件。
6. 每次交互只更新时间戳和 pending focus。
7. 当同一 focus 停止交互超过 `idle_seconds`，触发 `interaction_idle`。
8. 对同一 focus 的重复内容做去重或差异判断，避免高频上传。

手机端如果没有截图差异比较，也可以用文本 diff：

- 保存上一条 `text`。
- 新文本和上一条相同则跳过。
- 新文本变化过小可以跳过。
- 页面切换时始终保留第一条。

## 13. 手机端最小可兼容版本

要先跑通服务端链路，手机端最小实现只需要：

1. 能拿到当前前台 app/page 标识。
2. 能拿到一段页面文本。
3. 生成唯一 `event_id`。
4. 生成 `ts` Unix 时间戳。
5. POST 到 `/ingest`。

最小事件：

```json
{
  "source": "android_a",
  "device_id": "android_device_001",
  "events": [
    {
      "event_id": "0f4e2e72-0000-4000-8000-000000000001",
      "type": "focused_window_ocr",
      "ts": 1783502400.123,
      "data": {
        "trigger": "focus_switch",
        "focus_id": "com.example.app/.MainActivity",
        "app": "com.example.app",
        "title": "",
        "text": "页面文本"
      }
    }
  ]
}
```

服务端不会强制检查 `data` 内部字段，只要 envelope 满足要求即可。
