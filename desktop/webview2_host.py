#!/usr/bin/env python3
"""webview2_host.py —— 纯 ctypes 的 WebView2 原生嵌入（仅 Windows）。

为什么不用 pywebview / QtWebEngine（实测结论，勿走回头路）：
  - pywebview 的 Windows 后端依赖 pythonnet，其 clr_loader 只带 amd64/x86 的
    ClrLoader.dll，原生 ARM64 Python 进程加载报 0xc1（坏可执行格式）；
  - PySide6 虽有 win_arm64 轮子，但其中不含 QtWebEngine（无 Qt6WebEngine DLL）；
  - 本模块用原始 ctypes 手写 COM vtable，直接驱动系统自带的 Evergreen
    WebView2 运行时（Win10/11 原生自带，含 ARM64 原生版），无任何第三方依赖，
    arm64 / x64 一个实现通吃。

实现要点（与 SDK 头文件逐槽位核对过，勿凭记忆改；含 IUnknown 的 0-2 槽）：
  ICoreWebView2Environment : slot3 CreateCoreWebView2Controller
  ICoreWebView2Controller  : slot4 put_IsVisible, slot6 put_Bounds,
                             slot24 Close, slot25 get_CoreWebView2
  ICoreWebView2            : slot5 Navigate, slot6 NavigateToString,
                             slot30 CapturePreview
  Environment/Controller CompletedHandler : slot3 Invoke
  （依据：WebView2.h 的 C 风格 vtbl 结构体声明顺序）
"""
import ctypes
import json
import os
import platform
import threading
from ctypes import POINTER, WINFUNCTYPE, byref, c_int, c_long, c_uint32, c_void_p, c_wchar_p

HRESULT = c_long
S_OK = 0
E_NOINTERFACE = -2147467262        # 0x80004002
E_FAIL = -2147467259               # 0x80004005


def _guid_le(s):
    """'{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}' -> 16 字节内存序（含混合端序）。"""
    s = s.strip("{}").replace("-", "")
    d1 = bytes.fromhex(s[:8])[::-1]
    d2 = bytes.fromhex(s[8:12])[::-1]
    d3 = bytes.fromhex(s[12:16])[::-1]
    d4 = bytes.fromhex(s[16:])
    return d1 + d2 + d3 + d4


IID_IUNKNOWN = _guid_le("00000000-0000-0000-C000-000000000046")
IID_WEBVIEW = _guid_le("76ECEACB-0462-4D94-AC83-423A6793775E")
IID_CONTROLLER = _guid_le("4D00C0D1-9434-4EB6-8078-8697A560334F")
IID_CTRL_HANDLER = _guid_le("6C4819F3-C9B7-4260-8127-C9F5BDE7F68C")
IID_ENV_HANDLER = _guid_le("4E8A3389-C9D8-4BD2-B6B5-124FEE6CC14D")
IID_EXEC_HANDLER = _guid_le("49511172-CC67-4BCA-9923-137112F4C4CC")
IID_WEBMSG_HANDLER = _guid_le("57213F19-00E6-49FA-8E07-898EA01ECBD2")


class RECT(ctypes.Structure):
    _fields_ = [("left", c_long), ("top", c_long), ("right", c_long),
                ("bottom", c_long)]


def loader_arch():
    m = platform.machine().lower()
    return "arm64" if m in ("arm64", "aarch64") else "x64"


def load_loader_dll():
    """加载与进程架构匹配的 WebView2Loader.dll（桌面壳 assets 内置）。"""
    import sys
    name = "WebView2Loader-%s.dll" % loader_arch()
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [here, os.path.dirname(here),
             getattr(sys, "_MEIPASS", None), getattr(sys, "_MEIPASS2", None)]
    for root in filter(None, roots):
        path = os.path.join(root, "assets", "webview2", name)
        if os.path.isfile(path):
            return ctypes.WinDLL(path)
        path = os.path.join(root, "webview2", name)   # spec 打包路径
        if os.path.isfile(path):
            return ctypes.WinDLL(path)
    raise OSError("WebView2Loader not found: %s" % name)


def com_call(ptr, slot, restype, argspec, *args):
    """调用 COM 指针 vtable 上第 slot 个方法（含 IUnknown 0-2 槽）。"""
    vtbl = ctypes.cast(ptr, POINTER(c_void_p)).contents.value
    fn = ctypes.cast(vtbl, POINTER(c_void_p))[slot]
    proto = WINFUNCTYPE(restype, c_void_p, *argspec)
    return proto(fn)(ptr, *args)


def make_handler(iid_bytes, invoke_py, proto):
    """造一个 IUnknown + Invoke 的 COM 回调对象。

    返回 (interface_ptr_int, keepalive)：指针以纯 int 返回（传给 c_void_p
    参数即可），底层 vtable/回调/对象内存全部挂进 keepalive——调用方必须
    持有至回调完成，否则对象被 GC 后加载器手里就是悬垂指针（实测表现为
    hr=S_OK 但 Invoke 永远不来）。
    QueryInterface 只应答 IUnknown 与自身 IID（加载器调用约定下够用）。
    """
    QI = WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p)
    REF = WINFUNCTYPE(c_uint32, c_void_p)
    INVOKE = proto

    def _qi(this, riid, ppv):
        try:
            want = ctypes.string_at(riid, 16)
        except Exception:
            return E_NOINTERFACE
        if want in (iid_bytes, IID_IUNKNOWN):
            ctypes.memmove(ppv, byref(c_void_p(this)), ctypes.sizeof(c_void_p))
            _ref(this)
            return S_OK
        return E_NOINTERFACE

    def _ref(this):
        return 1

    qi_cb = QI(_qi)
    ref_cb = REF(_ref)
    rel_cb = REF(_ref)
    inv_cb = INVOKE(invoke_py)
    vtable = (c_void_p * 4)(*(ctypes.cast(cb, c_void_p).value
                              for cb in (qi_cb, ref_cb, rel_cb, inv_cb)))
    obj = (c_void_p * 1)(ctypes.cast(vtable, c_void_p).value)
    ptr_value = ctypes.cast(obj, c_void_p).value
    keepalive = (obj, vtable, qi_cb, ref_cb, rel_cb, inv_cb)
    return ptr_value, keepalive


class WebView2Host(object):
    """把 WebView2 嵌入给定 HWND（可为 tkinter Frame 的 winfo_id()）。

    用法（UI 线程，且有消息泵——tkinter mainloop 即可）：
        host = WebView2Host(hwnd, user_data_folder)
        host.start(on_ready=...)          # 异步：完成回调经消息泵触发
        host.navigate(url) / host.resize()
    """

    def __init__(self, hwnd, user_data_folder, on_error=None):
        self.hwnd = int(hwnd)
        self.user_data = user_data_folder
        self.on_error = on_error
        self._env = None            # ICoreWebView2Environment*
        self._controller = None     # ICoreWebView2Controller*
        self._webview = None        # ICoreWebView2*
        self._keep = []             # COM 回调与其 vtable 的引用，防 GC
        self._lock = threading.Lock()
        self._nav_guard_ok = False  # harden_navigation 是否成功（诊断/测试用）

    # -- 回调（都在 UI 线程经消息泵触发） ------------------------------------
    def _on_env(self, this, hr, env):
        if hr != S_OK or not env:
            self._fail("WebView2 环境创建失败 hr=0x%08X" % (hr & 0xFFFFFFFF))
            return hr
        # 完成回调交给我们的接口指针是「未 AddRef 的借用指针」，回调返回后
        # 上游即释放——要长期持有必须自己 +1，否则之后任何调用都是
        # use-after-free（实测：延迟数秒后调用直接段错误）。
        com_call(env, 1, c_uint32, [])             # IUnknown::AddRef
        self._env = env
        # ControllerCompletedHandler::Invoke(HRESULT, ICoreWebView2Controller*)
        # hr 参数必须是 HRESULT（c_long）——用 c_void_p 接 S_OK 会得到 None。
        ctrl_proto = WINFUNCTYPE(HRESULT, c_void_p, HRESULT, c_void_p)
        ctrl_handler, keep = make_handler(
            IID_CTRL_HANDLER, self._on_controller, ctrl_proto)
        self._keep.extend(keep)
        com_call(self._env, 3, HRESULT, [c_void_p, c_void_p],
                 c_void_p(self.hwnd), ctrl_handler)
        return hr

    def _on_controller(self, this, hr, controller):
        if hr != S_OK or not controller:
            self._fail("WebView2 控制器创建失败 hr=0x%08X" % (hr & 0xFFFFFFFF))
            return hr
        self._controller = controller
        com_call(controller, 1, c_uint32, [])      # AddRef（理由同 _on_env）
        com_call(self._controller, 4, HRESULT, [c_int], 1)     # put_IsVisible
        self.resize()
        wv = c_void_p()
        com_call(self._controller, 25, HRESULT, [POINTER(c_void_p)], byref(wv))
        self._webview = wv.value                   # getter 已含 +1 引用
        # ⚠️ harden_navigation() 含跨进程 COM 调用，绝不能在 ControllerCompleted
        # 回调栈上执行（会泵消息、破坏本线程 Python 状态 → Fatal
        # PyEval_RestoreThread，进程无痕消失）。由调用方在 UI 事件循环里调，
        # 见 run_windows_shell 的 root.after 与 _test_nav_guard.py。
        if self._on_created:
            try:
                self._on_created()
            except Exception as exc:
                self._fail("on_ready 回调异常: %r" % exc)
        return hr

    def _on_created(self):
        return None

    def _fail(self, message):
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                pass
        else:
            print("[webview2] %s" % message)

    # -- 公开接口 ------------------------------------------------------------
    def start(self, on_created=None, on_error=None):
        """发起异步创建；完成事件经 UI 消息泵回调。"""
        # WebView2 要求调用线程已完成 COM 初始化（STA）。tkinter 未必已调
        # OleInitialize，这里补一次：已初始化时返回 S_FALSE / 变体模式，均无害。
        coinit = ctypes.windll.ole32.CoInitializeEx
        coinit.argtypes = [c_void_p, c_uint32]
        coinit(None, 0x2)            # COINIT_APARTMENTTHREADED
        if on_created:
            self._on_created = on_created
        if on_error:
            self.on_error = on_error
        env_proto = WINFUNCTYPE(HRESULT, c_void_p, HRESULT, c_void_p)
        env_handler, keep = make_handler(IID_ENV_HANDLER, self._on_env, env_proto)
        self._keep.extend(keep)
        loader = load_loader_dll()
        fn = loader.CreateCoreWebView2EnvironmentWithOptions
        fn.restype = HRESULT
        fn.argtypes = [c_wchar_p, c_wchar_p, c_void_p, c_void_p]
        hr = fn(None, c_wchar_p(self.user_data), None, env_handler)
        if hr != S_OK:
            self._fail("CreateCoreWebView2EnvironmentWithOptions "
                       "hr=0x%08X" % (hr & 0xFFFFFFFF))
        return hr

    def add_init_script(self, js):
        """⚠️ 已弃用——AddScriptToExecuteOnDocumentCreated（slot 27）会同步
        跨进程并在内部泵消息：在 ControllerCompleted 回调里调用直接 access
        violation，在 tk 事件回调里调用会嵌套泵消息打乱 Tcl/ctypes 的 GIL
        状态（Fatal: PyEval_RestoreThread ... thread state is NULL）。
        注入脚本一律改走 execute_script（导航完成后调用，实测安全）。
        保留本方法仅为向后兼容提示，调用即抛错。"""
        raise RuntimeError(
            "add_init_script crashes re-entrantly; use execute_script() "
            "after navigation instead (see comment above)")

    def set_color_scheme(self, scheme):
        """0=跟随系统 1=浅色 2=深色（ICoreWebView2_13::get_Profile 槽 105 →
        ICoreWebView2Profile::put_PreferredColorScheme 槽 9）。

        默认 Auto 本就跟随系统，通常无需调用；仅在需要强制时用。
        失败静默（旧 runtime 无 Profile 时保持默认）。"""
        if not self._webview:
            return
        try:
            profile = c_void_p()
            com_call(self._webview, 105, HRESULT,
                     [POINTER(c_void_p)], byref(profile))
            if not profile:
                return
            try:
                com_call(profile, 9, HRESULT, [c_int], int(scheme))
            finally:
                com_call(profile, 2, c_uint32, [])   # Release
        except Exception:
            pass

    def on_web_message(self, callback):
        """页面 window.chrome.webview.postMessage(...) → callback(dict)。

        ICoreWebView2::add_WebMessageReceived（槽 34）+ 事件处理器的
        Invoke(槽 3)，载荷经 args::get_WebMessageAsJson（槽 4，LPWSTR 需
        CoTaskMemFree）。看板设置页借此把开关状态回传给桌面壳。
        """
        if not self._webview:
            return
        proto = WINFUNCTYPE(HRESULT, c_void_p, c_void_p, c_void_p)

        def _invoke(this, sender, args):
            try:
                raw = c_void_p()
                com_call(args, 4, HRESULT, [POINTER(c_void_p)], byref(raw))
                if raw:
                    try:
                        text = ctypes.wstring_at(raw)
                    finally:
                        ctypes.windll.ole32.CoTaskMemFree(raw)
                else:
                    text = "null"
                try:
                    payload = json.loads(text)
                except Exception:
                    payload = {"raw": text}
                callback(payload)
            except Exception as exc:
                self._fail("web message 回调异常: %r" % exc)
            return S_OK

        handler, keep = make_handler(IID_WEBMSG_HANDLER, _invoke, proto)
        self._keep.extend(keep)
        token = c_void_p()
        com_call(self._webview, 34, HRESULT, [c_void_p, POINTER(c_void_p)],
                 handler, byref(token))

    def post_message(self, obj):
        """壳 → 页面：ICoreWebView2::PostWebMessageAsJson（槽 32）。
        页面用 window.addEventListener('message', ...) 收（event.data 为
        已解析对象）。"""
        if self._webview:
            try:
                com_call(self._webview, 32, HRESULT, [c_wchar_p],
                         c_wchar_p(json.dumps(obj, ensure_ascii=False)))
            except Exception:
                pass

    def navigate(self, url):
        if self._webview:
            com_call(self._webview, 5, HRESULT, [c_wchar_p], c_wchar_p(url))

    def navigate_to_string(self, html):
        if self._webview:
            com_call(self._webview, 6, HRESULT, [c_wchar_p], c_wchar_p(html))

    def harden_navigation(self):
        """屏蔽返回手势、双指缩放与浏览器快捷键（无回调注册，纯 Settings 写）。

        背景：壳先用 NavigateToString 显示网关开启动画，就绪后导航到看板；
        触控板横扫/鼠标侧键（或 Alt+←）会把 WebView 回退到启动动画——那里
        只有"等网关"逻辑，不会自动前进，界面就此卡死，用户无法回到主界面。

        做法（ICoreWebView2Settings）：
          - put_IsSwipeNavigationEnabled(FALSE)（槽 32）：关闭触控板横扫
            前进/后退与鼠标侧键导航；
          - put_AreBrowserAcceleratorKeysEnabled(FALSE)（槽 24）：关闭 F5/
            Ctrl+P/Alt+←→ 等浏览器快捷键（看板有自身刷新，影响可接受）；
          - put_IsPinchZoomEnabled(FALSE)（槽 30）：关闭双指捏合缩放
            （误触会整页缩放变形）。
        写后**读回验证**（get 槽 23/29/31），设置没被上游接受时日志立现。
        另配合壳侧 WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS 注入
        --overscroll-history-navigation=0 --disable-pinch（Chromium 层
        双保险，Settings 覆盖不到的滑动路径在这里封死）。

        ⚠️ 历史教训（勿走回头路）：曾尝试 add_NavigationStarting 拦截回退
        导航——WebView2 在**关闭/拆卸阶段**仍会投递事件，Python 回调在解释
        器 finalizing 时被调用 → Fatal PyEval_RestoreThread 随机崩溃（实测
        即使回调体为空也必崩）。事件注册类防护在此壳里一律禁用。

        返回全部设置是否都成功。"""
        if not self._webview:
            return False
        ok = []
        settings = c_void_p()
        try:
            # get_Settings 在 ICoreWebView2（槽 3）上——controller 槽 3 是
            # get_IsVisible，打错对象会把 BOOL 当指针用（真机访问违例）。
            hr = com_call(self._webview, 3, HRESULT,
                          [POINTER(c_void_p)], byref(settings))
            ok.append(hr == S_OK and bool(settings))
            if settings:
                hr_sw = com_call(settings, 32, HRESULT, [c_int], 0)   # swipe
                hr_key = com_call(settings, 24, HRESULT, [c_int], 0)  # accel keys
                hr_pinch = com_call(settings, 30, HRESULT, [c_int], 0)  # pinch zoom
                # 触控板捏合在 Chromium 里转成 Ctrl+滚轮缩放——IsPinchZoomEnabled
                # 只管合成器 pinch，控制后者的是 IsZoomControlEnabled（槽 18）
                hr_zoom = com_call(settings, 18, HRESULT, [c_int], 0)
                ok.append(hr_sw == S_OK)
                ok.append(hr_key == S_OK)
                ok.append(hr_pinch == S_OK)
                ok.append(hr_zoom == S_OK)
                if hr_sw != S_OK or hr_key != S_OK or hr_pinch != S_OK \
                        or hr_zoom != S_OK:
                    print("[webview2] settings hr swipe=0x%08X accel=0x%08X "
                          "pinch=0x%08X zoom=0x%08X"
                          % (hr_sw & 0xFFFFFFFF, hr_key & 0xFFFFFFFF,
                             hr_pinch & 0xFFFFFFFF, hr_zoom & 0xFFFFFFFF))
                # 读回验证（get 槽：accel 23 / pinch 29 / swipe 31 / zoom 17）——
                # 误触复发时日志能立刻看出设置是否真被上游接受
                for name, get_slot in (("swipe", 31), ("accel", 23),
                                       ("pinch", 29), ("zoom", 17)):
                    try:
                        val = c_int(1)
                        hr_g = com_call(settings, get_slot, HRESULT,
                                        [POINTER(c_int)], byref(val))
                        if hr_g == S_OK and val.value != 0:
                            print("[webview2] WARN: %s still ENABLED after "
                                  "put(FALSE)" % name)
                            ok.append(False)
                    except Exception:
                        pass
        except Exception as exc:
            print("[webview2] harden_navigation failed: %r" % exc)
        finally:
            if settings:
                com_call(settings, 2, c_uint32, [])     # Release
        self._nav_guard_ok = bool(ok) and all(ok)
        if not self._nav_guard_ok:
            print("[webview2] WARN: navigation hardening incomplete: %r" % ok)
        return self._nav_guard_ok

    def get_source(self):
        """ICoreWebView2::get_Source（slot 4）—— 当前 URL/诊断用。"""
        if not self._webview:
            return None
        p = c_void_p()
        com_call(self._webview, 4, HRESULT, [POINTER(c_void_p)], byref(p))
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            ctypes.windll.ole32.CoTaskMemFree(p)

    def get_visible(self):
        if not self._controller:
            return None
        val = c_int(0)
        com_call(self._controller, 3, HRESULT, [POINTER(c_int)], byref(val))
        return bool(val.value)

    def execute_script(self, js, on_result):
        """ICoreWebView2::ExecuteScript（slot 29，异步；结果 JSON 串回调）。"""
        if not self._webview:
            return
        proto = WINFUNCTYPE(HRESULT, c_void_p, HRESULT, c_wchar_p)

        def _invoke(this, hr, result):
            try:
                on_result(hr, result)
            except Exception:
                pass
            return S_OK

        handler, keep = make_handler(IID_EXEC_HANDLER, _invoke, proto)
        self._keep.extend(keep)
        com_call(self._webview, 29, HRESULT, [c_wchar_p, c_void_p],
                 c_wchar_p(js), handler)

    def capture(self, png_path):
        """把当前页面渲染帧写成 PNG（ICoreWebView2::CapturePreview, slot 30）。

        自证渲染结果用：不依赖桌面截屏（锁屏/遮挡都不影响）。同步返回；
        失败抛 OSError 并带 hr。回调查询：CapturePreview 要求可写且可定位的
        IStream，STGM_WRITE 的只写流在某些版本上会 E_FAIL，用 READWRITE。
        """
        if not self._webview:
            raise RuntimeError("webview not ready")
        shlwapi = ctypes.windll.shlwapi
        shlwapi.SHCreateStreamOnFileEx.restype = HRESULT
        shlwapi.SHCreateStreamOnFileEx.argtypes = [
            c_wchar_p, c_uint32, c_uint32, c_int, c_void_p, POINTER(c_void_p)]
        stream = c_void_p()
        STGM_CREATE, STGM_READWRITE = 0x1000, 0x2
        hr = shlwapi.SHCreateStreamOnFileEx(
            c_wchar_p(png_path), STGM_CREATE | STGM_READWRITE, 0x80, True,
            None, byref(stream))
        if hr != S_OK or not stream:
            raise OSError("SHCreateStreamOnFileEx hr=0x%08X" % (hr & 0xFFFFFFFF))
        try:
            # COREWEBVIEW2_CAPTURE_PREVIEW_IMAGE_FORMAT_PNG = 0
            hr = com_call(self._webview, 30, HRESULT, [c_uint32, c_void_p],
                          0, stream)
            if hr != S_OK:
                raise OSError("CapturePreview hr=0x%08X" % (hr & 0xFFFFFFFF))
            return True
        finally:
            com_call(stream, 2, c_uint32, [])   # IStream::Release

    def resize(self):
        """按父 HWND 的客户区大小重设浏览器边界（tk <Configure> 时调用）。"""
        if not self._controller:
            return
        rect = RECT()
        if ctypes.windll.user32.GetClientRect(self.hwnd, byref(rect)):
            com_call(self._controller, 6, HRESULT, [RECT], rect)

    def close(self):
        if self._controller:
            try:
                com_call(self._controller, 24, HRESULT, [])   # Close
            except Exception:
                pass
            self._webview = None
            self._controller = None
            self._env = None
