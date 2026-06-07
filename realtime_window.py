"""
realtime_window.py
实时焦点窗口 + 键盘输入监控（中英文双轨版 v2）

架构说明：
  - 主循环：50ms 间隔轮询当前窗口，按需刷新屏幕 + 刷新字符缓冲
  - pynput 线程：捕获底层按键流（英文字母/特殊键）
  - 管道服务线程：监听 ime_hook.dll 传来的中文字符串
  - UIA 回退线程：补充捕获不暴露 WM_CHAR 的现代应用中文提交文本
  - 字符合并策略：可见的 WM_CHAR 中文逐字到达，Python 端在 200ms 窗口内合并

中英文智能合并策略：
  当收到 IME 中文事件时，自动回溯删除 pynput 缓冲区中对应的拼音字母，
  实现 "gaibian" -> "改变" 的无缝替换，显示更贴近真实输入意图。
"""

import ctypes
import ctypes.wintypes
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from pynput import keyboard

# ─────────────────────────────────────────────────────────────
# Windows API
# ─────────────────────────────────────────────────────────────
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# ─────────────────────────────────────────────────────────────
# 命名管道配置
# ─────────────────────────────────────────────────────────────
PIPE_NAME = r"\\.\pipe\OpendogIMEHook"

# ─────────────────────────────────────────────────────────────
# 全局共享状态
# ─────────────────────────────────────────────────────────────
state_lock   = threading.Lock()
key_buffer   = []          # 每个元素是 (type, text)，type: 'ascii' | 'ime' | 'key'
MAX_BUF      = 80          # 屏幕最多显示 80 个输入历史单元
dirty_flag   = True

# 连续英文字母累积器（用于 IME 替换）
_ascii_run_len = 0          # 记录当前在 key_buffer 末尾有多少连续英文字符

# DLL 句柄（全局，防止被 GC）
_ime_dll = None

# ─────────────────────────────────────────────────────────────
# WM_CHAR 逐字符合并缓冲区
# 部分应用的中文会逐字通过 WM_CHAR 到达，需要在 Python 端把短时间内
# 到达的字符合并成一整段文字再处理。没有 WM_CHAR 的应用由 UIA 回退线程补充。
# ─────────────────────────────────────────────────────────────
_ime_char_pending = []      # 暂存逐字到达的中文字符
_ime_char_last_t  = 0.0     # 最后一个字符到达的时间戳
IME_CHAR_MERGE_WINDOW = 0.20  # 200ms 合并窗口
_recent_ime_commits = []     # (timestamp, text, source)，用于 DLL/UIA 双通道去重
IME_DEDUPE_WINDOW = 0.80


# ─────────────────────────────────────────────────────────────
# 获取当前焦点窗口信息
# ─────────────────────────────────────────────────────────────
def get_foreground_window_info():
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "Unknown", "Unknown"

    length = user32.GetWindowTextLengthW(hwnd)
    title_buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title_buf, length + 1)
    title = title_buf.value

    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    app_name = "Unknown"
    if pid.value > 0:
        hProcess = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if hProcess:
            path_buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(hProcess, 0, path_buf, ctypes.byref(size)):
                app_name = os.path.basename(path_buf.value)
            kernel32.CloseHandle(hProcess)

    return app_name, title


# ─────────────────────────────────────────────────────────────
# 将一段中文文本写入 key_buffer（含拼音回退逻辑）
# ─────────────────────────────────────────────────────────────
def _commit_ime_text(text, source="dll"):
    """将中文上屏文本写入 key_buffer，并清除对应的拼音残留。
    调用时必须已持有 state_lock。"""
    global _ascii_run_len, dirty_flag, _recent_ime_commits

    now = time.time()
    _recent_ime_commits = [
        (timestamp, committed_text, commit_source)
        for timestamp, committed_text, commit_source in _recent_ime_commits
        if now - timestamp < IME_DEDUPE_WINDOW
    ]
    if any(
        committed_text == text and commit_source != source
        for _, committed_text, commit_source in _recent_ime_commits
    ):
        return
    _recent_ime_commits.append((now, text, source))

    # 回溯删除末尾的连续拼音字母
    for _ in range(_ascii_run_len):
        if key_buffer and key_buffer[-1][0] == 'ascii':
            key_buffer.pop()
    _ascii_run_len = 0

    # 插入中文（标记为 'ime' 类型）
    key_buffer.append(('ime', text))

    # 限制缓冲大小
    while len(key_buffer) > MAX_BUF:
        key_buffer.pop(0)

    dirty_flag = True


def _get_inserted_text(previous, current):
    """返回一次文本变化中新插入的片段；删除操作返回空字符串。"""
    if previous == current:
        return ""

    prefix_len = 0
    max_prefix_len = min(len(previous), len(current))
    while prefix_len < max_prefix_len and previous[prefix_len] == current[prefix_len]:
        prefix_len += 1

    suffix_len = 0
    max_suffix_len = min(len(previous) - prefix_len, len(current) - prefix_len)
    while (
        suffix_len < max_suffix_len
        and previous[len(previous) - suffix_len - 1] == current[len(current) - suffix_len - 1]
    ):
        suffix_len += 1

    end = len(current) - suffix_len if suffix_len else len(current)
    return current[prefix_len:end]


def _get_uia_control_id(control):
    try:
        return (
            control.NativeWindowHandle,
            tuple(control.GetRuntimeId()),
        )
    except Exception:
        return (
            getattr(control, "NativeWindowHandle", 0),
            getattr(control, "Name", ""),
            getattr(control, "ControlTypeName", ""),
        )


def _read_focused_uia_text(control):
    """读取控件公开的文本。UIA 不可用或密码框时返回 None。"""
    try:
        if getattr(control, "IsPassword", False):
            return None

        try:
            return control.GetValuePattern().Value
        except Exception:
            pass

        try:
            return control.GetTextPattern().DocumentRange.GetText(4096)
        except Exception:
            pass

        try:
            legacy_value = control.GetLegacyIAccessiblePattern().Value
            return legacy_value if legacy_value else None
        except Exception:
            return None
    except Exception:
        return None


def _find_uia_text_source(control, max_depth=6):
    """从焦点元素向父级查找真正公开文本模式的控件。"""
    for _ in range(max_depth):
        if not control:
            return None, None

        text = _read_focused_uia_text(control)
        if text is not None:
            return _get_uia_control_id(control), text

        try:
            control = control.GetParentControl()
        except Exception:
            return None, None

    return None, None


def uia_text_monitor_thread():
    """补充捕获不暴露 WM_CHAR 的现代应用中文提交文本。"""
    try:
        import uiautomation as auto
    except ImportError:
        print("[警告] 未安装 uiautomation，将跳过现代应用中文捕获回退。")
        return

    last_control = None
    last_text = None

    while True:
        try:
            control = auto.GetFocusedControl()
            if not control:
                time.sleep(0.10)
                continue

            control_id, text = _find_uia_text_source(control)
            if text is None:
                last_control = control_id
                last_text = None
                time.sleep(0.10)
                continue

            if control_id != last_control or last_text is None:
                last_control = control_id
                last_text = text
                time.sleep(0.10)
                continue

            inserted_text = _get_inserted_text(last_text, text)
            last_text = text

            # ASCII 仍由 pynput 记录。这里仅补充输入法提交文本，避免双重记录。
            if inserted_text and any(ord(ch) >= 0x80 for ch in inserted_text):
                with state_lock:
                    _commit_ime_text(inserted_text, source="uia")
        except Exception:
            # UIA 支持程度取决于目标应用，单次失败不应终止主监听。
            pass

        time.sleep(0.10)


# ─────────────────────────────────────────────────────────────
# 刷新 WM_CHAR 字符缓冲区（由主循环定期调用）
# ─────────────────────────────────────────────────────────────
def flush_ime_char_buffer():
    """检查 WM_CHAR 逐字缓冲区，如果超过合并窗口时间就提交。
    调用时必须已持有 state_lock。"""
    global _ime_char_pending, _ime_char_last_t

    if not _ime_char_pending:
        return

    # 还在合并窗口内，等更多字符到达
    if time.time() - _ime_char_last_t < IME_CHAR_MERGE_WINDOW:
        return

    # 窗口已过，把积攒的字符合并成一个完整的中文段落提交
    merged = "".join(_ime_char_pending)
    _ime_char_pending = []
    _ime_char_last_t = 0.0

    _commit_ime_text(merged)


# ─────────────────────────────────────────────────────────────
# pynput 键盘回调（在 pynput 内部线程里执行）
# ─────────────────────────────────────────────────────────────
def on_press(key):
    global dirty_flag, _ascii_run_len
    with state_lock:
        try:
            if hasattr(key, 'char') and key.char is not None and key.char.isprintable():
                # 普通可打印字符（含英文字母、数字、标点）
                key_buffer.append(('ascii', key.char))
                _ascii_run_len += 1
            else:
                raise AttributeError
        except AttributeError:
            # 特殊功能键
            if key == keyboard.Key.space:
                key_buffer.append(('ascii', ' '))
                _ascii_run_len += 1
            elif key == keyboard.Key.enter:
                key_buffer.append(('key', ' [Enter] '))
                _ascii_run_len = 0
            elif key == keyboard.Key.backspace:
                # 尝试原地回退
                if key_buffer and key_buffer[-1][0] == 'ascii':
                    key_buffer.pop()
                    _ascii_run_len = max(0, _ascii_run_len - 1)
                else:
                    key_buffer.append(('key', '[Backspace]'))
                    _ascii_run_len = 0
            elif key == keyboard.Key.tab:
                key_buffer.append(('key', '[Tab]'))
                _ascii_run_len = 0
            elif key == keyboard.Key.esc:
                key_buffer.append(('key', '[Esc]'))
                _ascii_run_len = 0
            else:
                name = key.name if hasattr(key, 'name') else str(key)
                # 过滤掉 ctrl/shift/alt 这类修饰键，太噪了
                if name not in ('ctrl_l', 'ctrl_r', 'shift', 'shift_r',
                                'alt_l', 'alt_r', 'alt_gr', 'caps_lock',
                                'cmd', 'cmd_r', 'num_lock', 'scroll_lock'):
                    key_buffer.append(('key', f'[{name}]'))
                    _ascii_run_len = 0

        # 维持缓冲池大小
        while len(key_buffer) > MAX_BUF:
            key_buffer.pop(0)
            if _ascii_run_len > 0:
                _ascii_run_len -= 1

        dirty_flag = True


# ─────────────────────────────────────────────────────────────
# 命名管道服务端（独立线程）
# 每次循环提供一个新实例，接收 DLL 送来的中文字符串
# ─────────────────────────────────────────────────────────────
def pipe_server_thread():
    """持续运行的命名管道服务线程"""
    global dirty_flag, _ime_char_pending, _ime_char_last_t
    import win32pipe
    import win32file
    import pywintypes

    PIPE_BUFFER_SIZE = 4096

    while True:
        try:
            hPipe = win32pipe.CreateNamedPipe(
                PIPE_NAME,
                win32pipe.PIPE_ACCESS_INBOUND,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES,
                PIPE_BUFFER_SIZE,
                PIPE_BUFFER_SIZE,
                0,
                None
            )
        except pywintypes.error:
            time.sleep(1)
            continue

        try:
            win32pipe.ConnectNamedPipe(hPipe, None)
        except pywintypes.error:
            win32file.CloseHandle(hPipe)
            continue

        try:
            while True:
                try:
                    _, data = win32file.ReadFile(hPipe, PIPE_BUFFER_SIZE)
                    text = data.decode('utf-8').strip()
                    if not text:
                        continue

                    with state_lock:
                        if len(text) == 1:
                            # ─── 单字符模式（来自 WM_CHAR）───
                            # 放入逐字缓冲区，等主循环合并后再提交
                            _ime_char_pending.append(text)
                            _ime_char_last_t = time.time()
                            dirty_flag = True
                        else:
                            # ─── 完整字符串模式（来自 IMM32/WM_IME_COMPOSITION）───
                            # 先清空逐字缓冲（如果有的话），直接提交
                            if _ime_char_pending:
                                merged = "".join(_ime_char_pending)
                                _ime_char_pending = []
                                _ime_char_last_t = 0.0
                                _commit_ime_text(merged)

                            _commit_ime_text(text)

                except pywintypes.error:
                    break
        finally:
            win32file.CloseHandle(hPipe)


# ─────────────────────────────────────────────────────────────
# 加载 DLL，注册全局钩子
# ─────────────────────────────────────────────────────────────
def load_ime_hook_dll():
    global _ime_dll
    dll_dir = Path(__file__).parent
    dll_path = dll_dir / "ime_hook_v3.dll"
    if not dll_path.exists():
        dll_path = dll_dir / "ime_hook.dll"
    if not dll_path.exists():
        print(f"[警告] 未找到 IME hook DLL，将跳过消息钩子中文捕获功能。")
        print(f"  请先进入 ime_hook/ 目录运行 build.bat 编译 DLL。")
        return False

    try:
        _ime_dll = ctypes.CDLL(str(dll_path))
        _ime_dll.InstallHook.restype = ctypes.c_bool
        result = _ime_dll.InstallHook()
        if result:
            print("[OK] ime_hook.dll 加载成功，中文 IME 拦截已激活。")
            return True
        else:
            print("[错误] InstallHook() 返回 False，钩子注册失败（可能权限不足）。")
            return False
    except Exception as e:
        print(f"[错误] 加载 DLL 失败：{e}")
        return False


def unload_ime_hook_dll():
    global _ime_dll
    if _ime_dll:
        try:
            _ime_dll.UninstallHook()
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# 屏幕渲染
# ─────────────────────────────────────────────────────────────
ANSI_RESET  = "\033[0m"
ANSI_IME    = "\033[93m"   # 黄色：输入法中文
ANSI_KEY    = "\033[90m"   # 灰色：功能键
ANSI_ASCII  = "\033[97m"   # 白色：英文/数字


def build_input_stream_display(buf):
    """把 key_buffer 渲染成带 ANSI 颜色的字符串"""
    parts = []
    for t, text in buf:
        if t == 'ime':
            parts.append(f"{ANSI_IME}{text}{ANSI_RESET}")
        elif t == 'key':
            parts.append(f"{ANSI_KEY}{text}{ANSI_RESET}")
        else:  # ascii
            parts.append(f"{ANSI_ASCII}{text}{ANSI_RESET}")
    return "".join(parts)


def clear_screen():
    print("\033[2J\033[H", end="")


def render(app, title, buf, ime_active):
    clear_screen()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    print("=" * 64)
    mode = "中英双轨" if ime_active else "英文/按键"
    print(f"实时监控 [{mode}]  ·  {timestamp}")
    print("=" * 64)

    print(f"\n\033[96m[当前焦点]\033[0m")
    print(f"  进程：{app}")
    print(f"  标题：{title}")

    print(f"\n\033[96m[实时输入流]\033[0m")
    stream = build_input_stream_display(buf)
    print(f"  输入：{stream}")

    if ime_active:
        print(f"\n  {ANSI_IME}■ 黄色{ANSI_RESET}=中文上屏  "
              f"{ANSI_ASCII}■ 白色{ANSI_RESET}=英文/数字  "
              f"{ANSI_KEY}■ 灰色{ANSI_RESET}=功能键")

    print("\n" + "=" * 64)
    print("按 Ctrl+C 退出")
    print("=" * 64)


# ─────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────
def main():
    global dirty_flag

    if os.name == 'nt':
        os.system('')  # 开启 Windows 终端 ANSI 支持

    print("=" * 64)
    print("正在初始化实时监控系统...")
    print("=" * 64)

    # 1. 尝试加载 IME 拦截 DLL
    ime_active = load_ime_hook_dll()

    # 2. 启动命名管道服务线程
    pipe_thread = threading.Thread(target=pipe_server_thread, daemon=True)
    pipe_thread.start()

    # 3. 启动 pynput 键盘监听线程
    kb_listener = keyboard.Listener(on_press=on_press)
    kb_listener.daemon = True
    kb_listener.start()

    # 4. 启动 UIA 文本变化监听，补充 TSF/自绘编辑器中不可见的中文提交
    uia_thread = threading.Thread(target=uia_text_monitor_thread, daemon=True)
    uia_thread.start()

    print("初始化完成，开始监控...\n")
    time.sleep(0.5)

    last_app   = None
    last_title = None

    try:
        while True:
            app, title = get_foreground_window_info()

            if app != last_app or title != last_title:
                dirty_flag = True
                last_app   = app
                last_title = title

            # ─── 定期检查 WM_CHAR 逐字缓冲区是否需要刷新 ───
            with state_lock:
                flush_ime_char_buffer()

            if dirty_flag:
                with state_lock:
                    buf_snapshot = list(key_buffer)
                    dirty_flag = False
                render(last_app, last_title, buf_snapshot, ime_active)

            time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n\n正在清理...")
        unload_ime_hook_dll()
        print("已安全退出。")


if __name__ == "__main__":
    main()
