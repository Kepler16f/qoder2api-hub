#!/usr/bin/env python3
"""trayicon.py —— Windows 系统托盘图标（纯 ctypes，零第三方依赖，仅 win32）。

实现：专用后台线程里建一个隐藏窗口 + Shell_NotifyIconW，消息泵收托盘事件，
经 sink 队列投递给 UI 线程（与网关事件同一通道，UI 侧 root.after 轮询即可）。
图标直接从可执行文件提取（ExtractIconEx），PyInstaller 打的 exe 图标即托盘
图标；提取失败回退系统默认图标。

事件：
    ("__EVENT__", "tray-left",  None)      左键（UI 侧自行决定显示/隐藏）
    ("__EVENT__", "tray-cmd",   cmd_id)    右键菜单选中项（菜单在托盘线程
                                           原生弹出，选中的命令 id 回传 UI）

右键菜单为原生 Win32 弹出菜单（TrackPopupMenu）：菜单生命周期、点击外部
消失都由系统负责。历史坑：早前用 tk 的 tk_popup 弹菜单，点击别处经常不
消失、残留在桌面上（Tk 弹出菜单在 Windows 上的已知缺陷），故改为原生。
必配的两步（MSDN KB135788）：弹出前 SetForegroundWindow(hwnd)，返回后
PostMessage(hwnd, WM_NULL)——缺任何一个菜单都会"点了别处也不消失"。

线程安全：wndproc 回调运行在托盘线程，只做 put 队列（put 线程安全），
绝不直接触碰 tk。
"""
import ctypes
import sys
import threading
from ctypes import POINTER, WINFUNCTYPE, byref, c_int, c_uint32, c_void_p
from ctypes import wintypes

WM_APP_TRAY = 0x8000 + 0x51          # WM_APP 区间自定义托盘回调消息
WM_QUIT = 0x0012
WM_DESTROY = 0x0002
WM_NULL = 0x0000
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_USER_TASKBAR_CREATED = None       # RegisterWindowMessage 结果，运行期填充

# 弹出菜单（全部按 64 位句柄/UINT_PTR 显式声明 argtypes，见 _run 里的教训）
MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100               # 直接返回选中项 id（不走 WM_COMMAND）


def enable_dark_menus():
    """让进程内的 Win32 经典菜单/弹窗跟随系统深色主题（未文档化导出）。

    SetPreferredAppMode(AllowDark=1) 在 uxtheme 序号 135，FlushMenuThemes
    在 136（Win10 1903+ 起）。老系统没有该导出，静默返回 False——不影响
    功能，只是菜单保持系统默认配色。
    """
    try:
        ux = ctypes.WinDLL("uxtheme")
        set_mode = ux[135]
        set_mode.restype = c_int
        set_mode.argtypes = [c_int]
        set_mode(1)                  # AllowDark：跟随系统主题
        flush = ux[136]
        flush.restype = None
        flush.argtypes = []
        flush()
        return True
    except Exception:
        return False


class _GUID(ctypes.Structure):
    """ctypes.wintypes.GUID 在 Python 3.13+ 已被移除，自带一份。"""
    _fields_ = [("Data1", c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


_LRESULT = ctypes.c_int64
_WNDPROC = WINFUNCTYPE(_LRESULT, wintypes.HWND, c_uint32,
                       wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASSW(ctypes.Structure):
    """ctypes.wintypes.WNDCLASSW 在 Python 3.13+ 已被移除，自带一份。"""
    _fields_ = [("style", c_uint32), ("lpfnWndProc", _WNDPROC),
                ("cbClsExtra", c_int), ("cbWndExtra", c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", c_uint32),
        ("hWnd", wintypes.HWND),
        ("uID", c_uint32),
        ("uFlags", c_uint32),
        ("uCallbackMessage", c_uint32),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", c_uint32),
        ("dwStateMask", c_uint32),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", c_uint32),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", c_uint32),
        ("guidItem", _GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class TrayIcon(object):
    """系统托盘图标。start() 后台线程建窗+挂图标；stop() 删图标并收线程。

    menu: 右键菜单项序列，元素为 (cmd_id, label) 或 None（分隔线）。
          在托盘线程里原生弹出，选中项经 sink 投递 ("tray-cmd", cmd_id)。
    """

    def __init__(self, sink, tip="Qoder2API-Hub", menu=None):
        self.sink = sink
        self.tip = tip
        self.menu = tuple(menu or ())
        self._thread = None
        self._tid = 0
        self._hwnd = None
        self._hicon = None
        self._stop = threading.Event()

    # -- 内部 ---------------------------------------------------------------
    def _make_icon(self):
        """托盘图标：优先解析应用自带的 qoder2api.ico（源码/冻结一致），
        失败再从 exe 提取（冻结产物即应用图标；源码运行会拿到 python 图标，
        这就是"托盘是 python 图标"的根因），最后回退系统默认。"""
        u32 = ctypes.windll.user32
        hicon = self._icon_from_ico()
        if not hicon:
            big = wintypes.HICON()
            small = wintypes.HICON()
            n = ctypes.windll.shell32.ExtractIconExW(
                sys.executable, 0, byref(big), byref(small), 1)
            if n > 0 and big:
                hicon = big
        if not hicon:
            hicon = u32.LoadIconW(None, wintypes.LPCWSTR(32512))
        self._hicon = hicon

    def _icon_from_ico(self):
        """解析 assets/qoder2api.ico 里的 PNG 条目 → CreateIconFromResourceEx
        → HICON（带 alpha，托盘显示应用的圆角渐变图标）。"""
        import os
        name = "qoder2api.ico"
        here = os.path.dirname(os.path.abspath(__file__))
        roots = [here, os.path.dirname(here),
                 getattr(sys, "_MEIPASS", None), getattr(sys, "_MEIPASS2", None)]
        ico_path = None
        for root in filter(None, roots):
            for cand in (os.path.join(root, "assets", name),
                         os.path.join(root, name)):
                if os.path.isfile(cand):
                    ico_path = cand
                    break
            if ico_path:
                break
        if not ico_path:
            return None
        try:
            with open(ico_path, "rb") as fh:
                data = fh.read()
            count = int.from_bytes(data[4:6], "little")
            entry32 = None
            for i in range(count):
                off = 6 + 16 * i
                e = data[off:off + 16]
                w = e[0] or 256
                size = int.from_bytes(e[8:12], "little")
                offset = int.from_bytes(e[12:16], "little")
                if w == 32:
                    entry32 = data[offset:offset + size]
                    break
            if not entry32:
                return None
            buf = ctypes.create_string_buffer(entry32, len(entry32))
            # CreateIconFromResourceEx(PBYTE, DWORD, BOOL fIcon, DWORD dwVer,
            #                          int cx, int cy, UINT Flags)
            u32 = ctypes.windll.user32
            u32.CreateIconFromResourceEx.restype = wintypes.HICON
            u32.CreateIconFromResourceEx.argtypes = [
                ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL,
                wintypes.DWORD, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            return u32.CreateIconFromResourceEx(buf, len(entry32), True,
                                                0x00030000, 0, 0, 0)
        except Exception:
            return None

    def _show_menu(self):
        """在托盘线程原生弹出右键菜单（TrackPopupMenu）。

        只在托盘线程调用：菜单属于创建它的线程，跨线程 Track 会拿不到
        输入、点了别处也不消失。"""
        u32 = ctypes.windll.user32
        hmenu = u32.CreatePopupMenu()
        if not hmenu:
            return
        cmd = 0
        try:
            for item in self.menu:
                if item is None:
                    u32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                else:
                    cmd_id, label = item
                    u32.AppendMenuW(hmenu, MF_STRING, int(cmd_id), label)
            pt = wintypes.POINT()
            u32.GetCursorPos(byref(pt))
            # 必须先抢前台（KB135788）：否则菜单不会随"点击别处"消失
            u32.SetForegroundWindow(self._hwnd)
            cmd = u32.TrackPopupMenu(
                hmenu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                pt.x, pt.y, 0, self._hwnd, None)
            # 把前台还回去，菜单的鼠标捕获才算彻底释放（否则仍可能残留）
            u32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            u32.DestroyMenu(hmenu)
        if cmd:
            self.sink.put(("__EVENT__", "tray-cmd", int(cmd)))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_APP_TRAY:
            if lparam == WM_LBUTTONUP:
                self.sink.put(("__EVENT__", "tray-left", None))
            elif lparam == WM_RBUTTONUP:
                self._show_menu()
            return 0
        if msg == WM_DESTROY:
            ctypes.windll.user32.PostQuitMessage(0)
            return 0
        if WM_USER_TASKBAR_CREATED is not None and msg == WM_USER_TASKBAR_CREATED:
            # explorer 重启后托盘图标会丢，重新挂一次
            self._notify(NIM_ADD)
            return 0
        return self._defwndproc(hwnd, msg, wparam, lparam)

    def _notify(self, code):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_APP_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self.tip
        return ctypes.windll.shell32.Shell_NotifyIconW(code, byref(nid))

    def _run(self):
        self._tid = threading.get_ident()
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        # ⚠️ 全部 API 必须显式声明 argtypes：缺省时 ctypes 按 32 位转换参数，
        # 64 位句柄（hInstance 等）会 OverflowError——托盘线程启动即崩的真凶
        # （表现为日志 "Exception in thread qoder-tray: OverflowError"、
        #  无托盘图标、关窗后程序不可见）。
        u32.DefWindowProcW.restype = _LRESULT
        u32.DefWindowProcW.argtypes = [wintypes.HWND, c_uint32,
                                       wintypes.WPARAM, wintypes.LPARAM]
        self._defwndproc = u32.DefWindowProcW
        u32.RegisterClassW.restype = ctypes.c_ushort
        u32.RegisterClassW.argtypes = [POINTER(_WNDCLASSW)]
        u32.CreateWindowExW.restype = wintypes.HWND
        u32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, wintypes.HMENU,
            wintypes.HINSTANCE, wintypes.LPVOID]
        u32.GetMessageW.restype = ctypes.c_int
        u32.GetMessageW.argtypes = [POINTER(wintypes.MSG), wintypes.HWND,
                                    wintypes.UINT, wintypes.UINT]
        u32.TranslateMessage.argtypes = [POINTER(wintypes.MSG)]
        u32.DispatchMessageW.restype = _LRESULT
        u32.DispatchMessageW.argtypes = [POINTER(wintypes.MSG)]
        u32.RegisterWindowMessageW.restype = wintypes.UINT
        u32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
        u32.GetCursorPos.argtypes = [POINTER(wintypes.POINT)]
        u32.PostQuitMessage.argtypes = [ctypes.c_int]
        u32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM]
        # 右键菜单 API：句柄/ID 都是 64 位，缺 argtypes 会被截断成 32 位
        u32.CreatePopupMenu.restype = wintypes.HMENU
        u32.AppendMenuW.restype = wintypes.BOOL
        u32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                    ctypes.c_size_t, wintypes.LPCWSTR]
        u32.TrackPopupMenu.restype = c_uint32
        u32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT,
                                       c_int, c_int, c_int, wintypes.HWND,
                                       c_void_p]
        u32.DestroyMenu.argtypes = [wintypes.HMENU]
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                     wintypes.WPARAM, wintypes.LPARAM]
        # GetModuleHandleW 缺 restype 时返回被截断的 int（低 32 位为负则变
        # 巨大无符号数），塞进 CreateWindowExW 的 hInstance 就 OverflowError
        # ——这正是"托盘线程一启动就崩"的根因（冻结环境 handle 高位随机命中）。
        k32.GetModuleHandleW.restype = ctypes.c_void_p
        k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        global WM_USER_TASKBAR_CREATED
        if WM_USER_TASKBAR_CREATED is None:
            WM_USER_TASKBAR_CREATED = u32.RegisterWindowMessageW("TaskbarCreated")

        wndproc = _WNDPROC(self._wndproc)
        wc = _WNDCLASSW()
        wc.style = 0x0008               # CS_DBLCLKS（占位；双击暂不用）
        wc.lpfnWndProc = wndproc
        wc.lpszClassName = "Qoder2APIHubTrayWnd"
        wc.hInstance = k32.GetModuleHandleW(None)
        atom = u32.RegisterClassW(byref(wc))
        if not atom:
            self.sink.put(("[tray] RegisterClassW failed\n",))
            return
        # 普通隐藏窗口（不 show）；message-only 窗口收不到 TaskbarCreated 广播
        self._hwnd = u32.CreateWindowExW(
            0, wintypes.LPCWSTR(atom), wintypes.LPCWSTR("Qoder2API-Hub tray"),
            0, 0, 0, 0, 0, None, None, wc.hInstance, None)
        if not self._hwnd:
            self.sink.put(("[tray] CreateWindowExW failed\n",))
            return
        self._make_icon()
        if not self._notify(NIM_ADD):
            self.sink.put(("[tray] Shell_NotifyIcon(NIM_ADD) failed\n",))
            return
        self.sink.put(("[tray] icon added to system tray\n",))

        msg = wintypes.MSG()
        while not self._stop.is_set():
            r = u32.GetMessageW(byref(msg), None, 0, 0)
            if r <= 0:
                break
            u32.TranslateMessage(byref(msg))
            u32.DispatchMessageW(byref(msg))

    # -- 公开 ---------------------------------------------------------------
    @property
    def alive(self):
        """托盘线程是否真的在跑（启动失败时要据此回退"关窗即退"）。"""
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="qoder-tray")
        self._thread.start()

    def stop(self, timeout=3.0):
        if not self._thread:
            return
        self._notify(NIM_DELETE)     # 任何线程调 Shell_NotifyIcon 都可以
        self._stop.set()
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(
                self._tid, WM_QUIT, 0, 0)
        self._thread.join(timeout)
        self._thread = None
