/**
 * ime_hook.cpp
 * 全局 WH_GETMESSAGE/WH_CALLWNDPROC 系统消息钩子 + IME 中文拦截器
 *
 * 核心变化 (v2)：
 *   除了原来的 WM_IME_COMPOSITION 拦截（仅对 IMM32 老应用有效），
 *   新增了 WM_CHAR 拦截（覆盖使用 TSF 框架的现代应用如 Chrome/QQ/VSCode）。
 *   使用时间窗口去重，避免在 IMM32 应用中同一段文字被重复上报两次。
 *
 * 编译依赖：user32.lib, imm32.lib
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <imm.h>
#include <string>

#pragma comment(lib, "imm32.lib")
#pragma comment(lib, "user32.lib")

// ─────────────────────────────────────────────────────────────
// 共享内存段：所有被注入的进程共享同一份 HHOOK
// ─────────────────────────────────────────────────────────────
#pragma data_seg(".shared")
volatile HHOOK g_hHook = NULL;
volatile HHOOK g_hCallWndHook = NULL;
#pragma data_seg()
#pragma comment(linker, "/SECTION:.shared,RWS")

// DLL 实例句柄（每个进程各持一份，不共享）
static HINSTANCE g_hDllInst = NULL;

// 每个进程各自的去重时间戳（不在共享段中）
// 当 WM_IME_COMPOSITION 成功提取了中文后，记录当前时间，
// 短时间内到达的 WM_CHAR 非 ASCII 字符将被视为重复而忽略。
static ULONGLONG g_lastImeCompTime = 0;
static HWND g_lastMessageHwnd = NULL;
static UINT g_lastMessage = 0;
static WPARAM g_lastMessageWParam = 0;
static LPARAM g_lastMessageLParam = 0;
static int g_lastMessageSource = 0;
static ULONGLONG g_lastMessageTime = 0;

// 命名管道名称
static const wchar_t* PIPE_NAME = L"\\\\.\\pipe\\OpendogIMEHook";

// ─────────────────────────────────────────────────────────────
// 内部工具：将文本写入命名管道
// ─────────────────────────────────────────────────────────────
static void SendToPipe(const std::wstring& text) {
    if (text.empty()) return;

    HANDLE hPipe = CreateFileW(
        PIPE_NAME,
        GENERIC_WRITE,
        0, NULL,
        OPEN_EXISTING,
        0, NULL
    );
    if (hPipe == INVALID_HANDLE_VALUE) return;

    // 转为 UTF-8
    int utf8_len = WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, NULL, 0, NULL, NULL);
    if (utf8_len > 1) {
        std::string utf8_str(utf8_len, '\0');
        WideCharToMultiByte(CP_UTF8, 0, text.c_str(), -1, &utf8_str[0], utf8_len, NULL, NULL);
        utf8_str.resize(utf8_len - 1);
        utf8_str += "\n";

        DWORD written = 0;
        WriteFile(hPipe, utf8_str.c_str(), (DWORD)utf8_str.size(), &written, NULL);
    }

    CloseHandle(hPipe);
}

// ─────────────────────────────────────────────────────────────
// 方法 1：从 WM_IME_COMPOSITION 提取（IMM32 应用，如微信）
// 优势：能一次性拿到完整的词/句，如 "你好世界"
// ─────────────────────────────────────────────────────────────
static void ExtractAndSendIMEResult(HWND hwnd) {
    HIMC hImc = ImmGetContext(hwnd);
    if (!hImc) return;

    LONG len = ImmGetCompositionStringW(hImc, GCS_RESULTSTR, NULL, 0);
    if (len > 0) {
        std::wstring result(len / sizeof(wchar_t), L'\0');
        ImmGetCompositionStringW(hImc, GCS_RESULTSTR, &result[0], len);
        SendToPipe(result);

        // 记录时间戳，用于去重 WM_CHAR
        g_lastImeCompTime = GetTickCount64();
    }

    ImmReleaseContext(hwnd, hImc);
}

// ─────────────────────────────────────────────────────────────
// 统一处理队列消息和直接发送到窗口过程的消息
// ─────────────────────────────────────────────────────────────
static void HandleMessage(HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam, int source) {
    ULONGLONG now = GetTickCount64();
    if (
        source != g_lastMessageSource
        && now - g_lastMessageTime < 25
        && hwnd == g_lastMessageHwnd
        && message == g_lastMessage
        && wParam == g_lastMessageWParam
        && lParam == g_lastMessageLParam
    ) {
        return;
    }

    g_lastMessageHwnd = hwnd;
    g_lastMessage = message;
    g_lastMessageWParam = wParam;
    g_lastMessageLParam = lParam;
    g_lastMessageSource = source;
    g_lastMessageTime = now;

    if (message == WM_IME_COMPOSITION) {
        if (lParam & GCS_RESULTSTR) {
            ExtractAndSendIMEResult(hwnd);
        }
    }
    else if (message == WM_CHAR || message == WM_UNICHAR) {
        wchar_t ch = (wchar_t)wParam;
        if (ch >= 0x80) {
            if (now - g_lastImeCompTime > 150) {
                std::wstring s(1, ch);
                SendToPipe(s);
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────
// WH_GETMESSAGE 钩子回调（被注入所有 UI 进程）
// ─────────────────────────────────────────────────────────────
LRESULT CALLBACK GetMsgProc(int nCode, WPARAM wParam, LPARAM lParam) {
    if (nCode == HC_ACTION && wParam == PM_REMOVE) {
        MSG* pMsg = reinterpret_cast<MSG*>(lParam);
        if (pMsg) {
            HandleMessage(pMsg->hwnd, pMsg->message, pMsg->wParam, pMsg->lParam, 1);
        }
    }

    return CallNextHookEx(g_hHook, nCode, wParam, lParam);
}

// 捕获未经过 GetMessage 队列、直接发送到窗口过程的输入消息。
LRESULT CALLBACK CallWndProc(int nCode, WPARAM wParam, LPARAM lParam) {
    if (nCode == HC_ACTION) {
        CWPSTRUCT* pMsg = reinterpret_cast<CWPSTRUCT*>(lParam);
        if (pMsg) {
            HandleMessage(pMsg->hwnd, pMsg->message, pMsg->wParam, pMsg->lParam, 2);
        }
    }

    return CallNextHookEx(g_hCallWndHook, nCode, wParam, lParam);
}

// ─────────────────────────────────────────────────────────────
// 导出接口
// ─────────────────────────────────────────────────────────────
extern "C" __declspec(dllexport) BOOL InstallHook() {
    if (g_hHook != NULL && g_hCallWndHook != NULL) return TRUE;

    g_hHook = SetWindowsHookEx(
        WH_GETMESSAGE,
        GetMsgProc,
        g_hDllInst,
        0
    );

    g_hCallWndHook = SetWindowsHookEx(
        WH_CALLWNDPROC,
        CallWndProc,
        g_hDllInst,
        0
    );

    if (g_hHook == NULL || g_hCallWndHook == NULL) {
        if (g_hHook != NULL) {
            UnhookWindowsHookEx(g_hHook);
            g_hHook = NULL;
        }
        if (g_hCallWndHook != NULL) {
            UnhookWindowsHookEx(g_hCallWndHook);
            g_hCallWndHook = NULL;
        }
        return FALSE;
    }

    return TRUE;
}

extern "C" __declspec(dllexport) void UninstallHook() {
    if (g_hHook != NULL) {
        UnhookWindowsHookEx(g_hHook);
        g_hHook = NULL;
    }
    if (g_hCallWndHook != NULL) {
        UnhookWindowsHookEx(g_hCallWndHook);
        g_hCallWndHook = NULL;
    }
}

// ─────────────────────────────────────────────────────────────
// DLL 入口
// ─────────────────────────────────────────────────────────────
BOOL WINAPI DllMain(HINSTANCE hInstance, DWORD dwReason, LPVOID) {
    if (dwReason == DLL_PROCESS_ATTACH) {
        g_hDllInst = hInstance;
        DisableThreadLibraryCalls(hInstance);
    }
    return TRUE;
}
