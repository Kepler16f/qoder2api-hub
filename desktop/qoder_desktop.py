#!/usr/bin/env python3
"""Qoder2API-Hub 桌面客户端壳 —— 内嵌看板 + 网关生命周期管理。

四种运行方式：
    python desktop/qoder_desktop.py              # 源码运行（需系统 Python 3.9+）
    Qoder2API-Hub(.exe / .app)                   # PyInstaller 冻结产物（免装 Python）
    python desktop/qoder_desktop.py --smoke-test # 无 GUI 冒烟测试（CI / 体检用）
    python desktop/qoder_desktop.py --ui console # 强制控制台 UI（不内嵌看板）

内嵌看板（窗口里就是网页版，功能 100% 对等，绝不依赖外部浏览器）：
    Windows : tkinter 窗口 + WebView2 原生嵌入（自研 ctypes COM 宿主
              webview2_host.py；arm64 / x64 原生通用，见该模块头注释）
    macOS   : pywebview（WKWebView，pip 纯轮子，无系统依赖）
    Linux   : PySide6 + QtWebEngine（x64 需 glibc≥2.28；arm64 需 glibc≥2.39）
    兜底    : 任一内嵌方案不可用时自动退回 tkinter 控制台 + 浏览器打开看板

设计要点：
  - 网关（qoder_proxy.main）在守护线程内运行；关闭窗口 = 优雅停机并退出，
    延续本项目「生命周期就是那个窗口」的哲学，不留后台残留进程。
  - 数据目录与代码分离（accounts/ usage/ 运行日志）：
      Windows / Linux  = 可执行文件同目录（便携模式；目录不可写时回退用户目录）
      macOS            = ~/Library/Application Support/qoder2api-hub
      源码运行          = 仓库根目录（与 .bat 启动行为一致）
      任何时候可用 QD_DATA_DIR 覆盖。
  - 冻结模式下网关以 __file__ 定位的只读资源（dashboard.html / baseprompt.json /
    模型快照）由 PyInstaller --add-data 落在 _MEIPASS，解析路径不变；可写目录
    通过 --accounts-dir / --usage-dir / QD_UMID_DIR 三个现成入口重定向，网关
    代码因此几乎零改动。
  - 启动前探测端口：已有本网关实例在跑则「附身」模式（窗口直接展示该实例的
    看板，不重复起服务）；被其它程序占用则明确报错，绝不静默换端口。
"""
import functools
import json
import os
import queue
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser

APP_NAME = "Qoder2API-Hub"
APP_TITLE = "Qoder2API-Hub"
DEFAULT_PORT = 8790
LOG_NAME = "desktop-gateway.log"
CONFIG_NAME = "desktop.json"

GREEN = "#1a7f37"
AMBER = "#8a6d3b"
RED = "#c62828"

# 顶层 try-import：既是惰性依赖声明，也让 PyInstaller 的模块分析能看到这些
# 包（按各平台构建环境装了什么就打什么，一个 spec 六平台通用）。
try:
    import webview2_host            # Windows 原生 WebView2 嵌入（本目录内模块）
except Exception:
    webview2_host = None
try:
    import trayicon                 # Windows 系统托盘（本目录内模块，纯 ctypes）
except Exception:
    trayicon = None
try:
    import webview as _pywebview    # macOS：WKWebView
except Exception:
    _pywebview = None
try:
    from PySide6 import QtCore as _qtc      # noqa: F401  Linux：QtWebEngine
    from PySide6 import QtWidgets as _qtwidgets  # noqa: F401
    from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
except Exception:
    _qtc = _qtwidgets = None
    QWebEngineView = None


# ---------------------------------------------------------------------------
# 启动页 / 错误页（内嵌窗口在网关就绪前展示；自包含、无外链）
# ---------------------------------------------------------------------------
_PAGE_TMPL = """<!doctype html><html><head><meta charset="utf-8">
<style>
 body{{margin:0;height:100vh;display:flex;flex-direction:column;justify-content:center;
      align-items:center;background:linear-gradient(160deg,#1E6FEB,#0DBD8B);
      color:#fff;font-family:'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}}
 h1{{font-size:34px;margin:0 0 8px}} .sub{{opacity:.85;font-size:15px}}
 .spin{{width:34px;height:34px;border:4px solid rgba(255,255,255,.35);
       border-top-color:#fff;border-radius:50%;animation:s 1s linear infinite;
       margin-bottom:22px}}
 @keyframes s{{to{{transform:rotate(360deg)}}}}
 .err{{background:rgba(0,0,0,.28);padding:14px 22px;border-radius:10px;
      margin-top:18px;max-width:70%;font-size:14px;line-height:1.7}}
 button{{margin-top:22px;padding:10px 30px;font-size:15px;border:0;border-radius:8px;
        background:#fff;color:#1E6FEB;cursor:pointer}}
 @media (prefers-color-scheme: dark){{
   body{{background:linear-gradient(160deg,#0d1730,#06342b)}}
   .err{{background:rgba(0,0,0,.45)}}
 }}
</style></head><body>{body}</body></html>"""

BOOT_HTML = _PAGE_TMPL.format(body="""
  <div class="spin"></div>
  <h1>Qoder2API-Hub</h1>
  <div class="sub">正在启动网关…</div>""")


def error_html(message, with_retry_js=False):
    button = ('<button onclick="pywebview.api.retry()">重试</button>'
              if with_retry_js else '')
    return _PAGE_TMPL.format(body="""
  <h1>网关未能启动</h1>
  <div class="err">%s<br>右键窗口可打开控制菜单（修改端口 / 重启网关）。</div>%s
""" % (message, button))


# 隐藏网页内容右侧滚动条（各内嵌壳共用；仅外观，滚轮/触摸滚动不受影响）。
# 注意：必须用 ExecuteScript 在导航完成后注入——AddScriptToExecuteOnDocument-
# Created 会同步跨进程并内部泵消息，在 tk 事件循环里调用会炸（见
# webview2_host.add_init_script 的注释），所以不能走初始脚本。
HIDE_SCROLLBAR_JS = (
    "(function(){function add(){if(document.getElementById('__qd_nosb'))return;"
    "var s=document.createElement('style');s.id='__qd_nosb';"
    "s.textContent='::-webkit-scrollbar{width:0;height:0}"
    "::-webkit-scrollbar-thumb{background:transparent}"
    "::-webkit-scrollbar-track{background:transparent}"
    "html{scrollbar-width:none}';"
    "(document.documentElement||document.head||document.body||document)"
    ".appendChild(s);}"
    "add();document.addEventListener('DOMContentLoaded',add);})()")

# 强制深色调色板（与 dashboard.html 的 @media dark 块同源；仅 QD_FORCE_DARK
# 验证用——正常情况交给 prefers-color-scheme 媒体查询自动跟随系统）。
DARK_VARS_JS = (
    "(function(){var s=document.createElement('style');s.id='__qd_dark';"
    "s.textContent=':root{--bg:#0b1220;--panel:#121a2b;--panel2:#0e1626;"
    "--panel3:#1b2438;--line:#22304d;--line-hover:#31426b;--fg:#e7eef8;"
    "--dim:#93a4bd;--dim-light:#6d7f9a;--accent:#4c8dff;--accent-hover:#6ea4ff;"
    "--accent-soft:rgba(76,141,255,.16);--accent2:#2dd47f;"
    "--accent2-soft:rgba(45,212,127,.16);--warn:#f0a63c;"
    "--warn-soft:rgba(240,166,60,.16);--think:#b39cff;"
    "--think-soft:rgba(179,156,255,.16);--bad:#ff7b7b;"
    "--bad-soft:rgba(255,123,123,.14);color-scheme:dark}"
    "header{background:rgba(11,18,32,.92)}"
    ".badge.off{color:#93a4bd;background:#1b2438;border-color:#31426b}"
    "button.sec{background:#1b2438}"
    ".modal{background:var(--panel)}"
    ".login-link:hover{background:#1b2438}"
    ".key-card{background:var(--panel)}"
    ".key-action-btn{background:#1b2438}"
    ".realm-card.active{background:var(--panel)}"
    ".toggle-realm-btn{background:#1b2438}"
    ".main-nav-btn:hover{background:#1b2438}"
    ".main-nav-btn.active{background:var(--panel2)}"
    "#analyticsKpiCards .card{background:var(--panel)}';"
    "(document.documentElement||document.body||document)"
    ".appendChild(s);})()")


def is_system_dark():
    """系统是否深色应用模式（Windows 注册表 / macOS defaults；Linux 交给
    Chromium/Qt 自行跟随平台主题，这里只用于窗口镶边与菜单配色）。"""
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion"
                    r"\Themes\Personalize") as key:
                return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
        if sys.platform == "darwin":
            import subprocess
            out = subprocess.run(
                ["defaults", "read", "-g", "AppleInterfaceStyle"],
                capture_output=True, text=True, timeout=3)
            return out.returncode == 0 and "Dark" in out.stdout
    except Exception:
        pass
    return False


def _apply_windows_titlebar_theme(hwnd, dark):
    """Windows 深色标题栏（DWMWA_USE_IMMERSIVE_DARK_MODE；旧 build 用 19）。"""
    import ctypes
    from ctypes import byref, c_int
    dwm = ctypes.windll.dwmapi
    for attr in (20, 19):
        val = c_int(1 if dark else 0)
        if dwm.DwmSetWindowAttribute(int(hwnd), attr, byref(val), 4) == 0:
            return True
    return False


def _inject_page_extras(root, host, cfg=None):
    """每次导航到看板后的注入编排：滚动条隐藏（+1.5s/+4s 两次，防页面重建）、
    桌面桥（__qdDesktop，看板「设置 → 桌面客户端」区块据此显示）；
    QD_FORCE_DARK=1 时另注入强制深色调色板（验证用）。"""

    def _run(js, tag):
        def _call():
            try:
                host.execute_script(
                    js, lambda hr, res: print("[desktop] inject %s hr=0x%08X"
                                              % (tag, hr & 0xFFFFFFFF)))
            except Exception as exc:
                print("[desktop] inject %s failed: %r" % (tag, exc))
        return _call

    root.after(1500, _run(HIDE_SCROLLBAR_JS, "nosb#1"))
    root.after(4000, _run(HIDE_SCROLLBAR_JS, "nosb#2"))
    if cfg is not None:
        root.after(600, _run(desktop_bridge_js(cfg), "dsk-bridge"))
    if (os.environ.get("QD_FORCE_DARK") or "").strip() in ("1", "true", "yes"):
        root.after(800, _run(DARK_VARS_JS, "dark"))


# ---------------------------------------------------------------------------
# 路径与环境（必须在 import qoder_proxy 之前完成 —— 网关在 import 期读环境变量）
# ---------------------------------------------------------------------------
def is_frozen():
    return bool(getattr(sys, "frozen", False))


def bundle_dir():
    """PyInstaller 单文件解包目录（只读资源所在地）；源码运行返回 desktop/。"""
    return getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def repo_dir():
    """仓库根目录（源码运行时网关代码与资源所在；冻结模式下仅用于兜底探测）。"""
    if is_frozen():
        return bundle_dir()
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_data_dir():
    """数据目录：QD_DATA_DIR > 平台约定（冻结）> 仓库根（源码）。"""
    env = (os.environ.get("QD_DATA_DIR") or "").strip()
    if env:
        return os.path.abspath(env)
    if is_frozen():
        if sys.platform == "darwin":
            return os.path.join(os.path.expanduser("~"), "Library",
                                "Application Support", "qoder2api-hub")
        # Windows / Linux：便携模式 —— 数据放可执行文件旁边，方便备份与搬运。
        portable = os.path.dirname(os.path.abspath(sys.executable))
        if os.access(portable, os.W_OK):
            return portable
        # exe 放在 Program Files 等只读位置时回退用户数据目录。
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or os.path.expanduser("~")
            return os.path.join(base, "qoder2api-hub")
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
        return os.path.join(xdg, "qoder2api-hub")
    return repo_dir()


def setup_environment(data_dir):
    """建目录、切工作目录、设置网关的重定向环境变量。返回各路径 dict。"""
    accounts = os.path.join(data_dir, "accounts")
    usage = os.path.join(data_dir, "usage")
    os.makedirs(accounts, exist_ok=True)
    os.makedirs(usage, exist_ok=True)
    os.chdir(data_dir)
    # 网关可写路径的现成覆盖入口：--accounts-dir / --usage-dir 由启动参数传，
    # 这里同步设 ACCOUNTS_DIR（qoder_accounts 的机器身份缓存、调度器状态目录
    # 直读该环境变量），保证桌面模式所有可写状态都落在数据目录。
    os.environ["ACCOUNTS_DIR"] = accounts
    os.environ["QD_PROXY_USAGE_DIR"] = usage
    # UMID 组件（CI 构建期由 _install_umid.py 提取、spec 打进包里；源码运行时
    # POSIX 下网关自己会找 <repo>/umid，Windows 只认本环境变量）。
    umid_name = "runtime-info.exe" if os.name == "nt" else "runtime-info"
    for root in (bundle_dir(), repo_dir()):
        cand = os.path.join(root, "umid", umid_name)
        if os.path.isfile(cand):
            os.environ["QD_UMID_DIR"] = os.path.dirname(cand)
            break
    return {"data": data_dir, "accounts": accounts, "usage": usage}


class Tee(object):
    """把 stdout/stderr 复制到 UI 队列 + 日志文件。

    PyInstaller --windowed 下 sys.stdout 可能为 None，网关的 print 会静默
    丢失；这里无论如何先接管，日志面板与日志文件才有内容。源码运行时镜像
    回真实 stderr，命令行调试不丢输出。
    """

    def __init__(self, sink, log_path, mirror):
        self._sink = sink
        self._lock = threading.Lock()
        self._mirror = mirror
        try:
            if os.path.getsize(log_path) > 5 * 1024 * 1024:
                old = log_path + ".old"
                if os.path.exists(old):
                    os.remove(old)
                os.replace(log_path, old)
        except OSError:
            pass
        try:
            self._fh = open(log_path, "a", encoding="utf-8", errors="replace")
        except OSError:
            self._fh = None

    def write(self, text):
        with self._lock:
            if self._fh:
                try:
                    self._fh.write(text)
                    self._fh.flush()
                except OSError:
                    pass
            try:
                self._sink.put_nowait(text)
            except Exception:
                pass
            if self._mirror:
                try:
                    self._mirror.write(text)
                except Exception:
                    pass
        return len(text)

    def flush(self):
        if self._fh:
            try:
                self._fh.flush()
            except OSError:
                pass

    def isatty(self):
        return False

    def reconfigure(self, *args, **kwargs):  # qoder_proxy 启动时会尝试调用
        pass


def install_tee(log_queue, log_path):
    """接管 stdout/stderr（Tee 镜像回真实 stderr 以便命令行调试）。"""
    mirror = sys.stderr if (sys.stderr and hasattr(sys.stderr, "write")) else None
    tee = Tee(log_queue, log_path, mirror)
    sys.stdout = tee
    sys.stderr = tee
    return tee


DEFAULT_CONFIG = {
    "port": DEFAULT_PORT, "lan": False, "auto_open": True,
    "tray": True,          # 关闭/最小化时收缩到系统托盘（Windows 壳）
    "autostart": False,    # 开机自动启动（跟随系统，安装版可用）
    "win_w": 0, "win_h": 0,  # 上次窗口尺寸（0 = 用默认）
}


def load_config(config_path):
    """读取桌面配置（缺省项自动补全；未知键忽略）。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path, encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            cfg.update({k: stored[k] for k in cfg if k in stored})
    except Exception:
        pass
    try:
        cfg["port"] = int(cfg["port"])
    except (TypeError, ValueError):
        cfg["port"] = DEFAULT_PORT
    return cfg


def save_config(config_path, cfg):
    try:
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump({k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG},
                      fh)
    except OSError:
        pass


AUTORUN_NAME = "Qoder2API-Hub"


def _autostart_plist_path():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents",
                        "io.github.shuishuipingan.qoder2api-hub.plist")


def _autostart_desktop_path():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME")
                        or os.path.join(os.path.expanduser("~"), ".config"),
                        "autostart", "qoder2api-hub.desktop")


def autostart_supported():
    """仅冻结产物支持（exe/app 路径明确）；源码运行不写自启动。"""
    return is_frozen()


def autostart_enabled():
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
                winreg.QueryValueEx(key, AUTORUN_NAME)
                return True
        if sys.platform == "darwin":
            return os.path.exists(_autostart_plist_path())
        if sys.platform.startswith("linux"):
            return os.path.exists(_autostart_desktop_path())
    except Exception:
        pass
    return False


def autostart_apply(enable):
    """写/删开机自启动项（Windows=HKCU Run；mac=LaunchAgent；Linux=XDG）。
    启动命令带 --hidden：开机自启后静默进托盘，不抢焦点。"""
    if not is_frozen():
        return False
    exe = os.path.abspath(sys.executable)
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0, winreg.KEY_SET_VALUE) as key:
                if enable:
                    winreg.SetValueEx(key, AUTORUN_NAME, 0, winreg.REG_SZ,
                                      '"%s" --hidden' % exe)
                else:
                    try:
                        winreg.DeleteValue(key, AUTORUN_NAME)
                    except FileNotFoundError:
                        pass
            return True
        if sys.platform == "darwin":
            import plistlib
            path = _autostart_plist_path()
            if enable:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                plistlib.dump({
                    "Label": "io.github.shuishuipingan.qoder2api-hub",
                    "ProgramArguments": [exe, "--hidden"],
                    "RunAtLoad": True}, open(path, "wb"))
            elif os.path.exists(path):
                os.remove(path)
            return True
        if sys.platform.startswith("linux"):
            path = _autostart_desktop_path()
            if enable:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("[Desktop Entry]\nType=Application\n"
                             "Name=Qoder2API-Hub\n"
                             "Exec=\"%s\" --hidden\n"
                             "X-GNOME-Autostart-enabled=true\n" % exe)
            elif os.path.exists(path):
                os.remove(path)
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# 网关生命周期
# ---------------------------------------------------------------------------
def probe_gateway(port, timeout=2.0):
    """探测端口上的网关。返回 "ours" / "foreign" / None。"""
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/health" % int(port), timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    # 只有本网关的 /health 带 "accounts" 字段（与 main() 的防双开判据一致）。
    if isinstance(body, dict) and "accounts" in body:
        return "ours"
    return "foreign"


class Gateway(object):
    """qoder_proxy.main 的线程封装：start / stop / 异常自报告。"""

    def __init__(self):
        self.thread = None
        self.exit_note = None      # 线程结束原因（给状态行的中文提示）
        self._lock = threading.Lock()

    def start(self, host, port, lan):
        with self._lock:
            if self.thread and self.thread.is_alive():
                return False
            self.exit_note = None

            def run():
                try:
                    import qoder_proxy
                    argv = ["--host", host, "--port", str(port),
                            "--accounts-dir", os.environ["ACCOUNTS_DIR"],
                            "--usage-dir", os.environ["QD_PROXY_USAGE_DIR"]]
                    if lan:
                        argv.append("--lan")
                    qoder_proxy.main(argv)
                except SystemExit as exc:
                    if exc.code:
                        self.exit_note = ("网关启动失败（exit=%s），详见日志"
                                          % exc.code)
                except Exception:
                    self.exit_note = "网关线程异常退出，详见日志"
                    traceback.print_exc()
                finally:
                    try:
                        import qoder_proxy
                        qoder_proxy.SERVER = None
                    except Exception:
                        pass

            self.thread = threading.Thread(target=run, daemon=True,
                                           name="qoder-gateway")
            self.thread.start()
            return True

    def stop(self, timeout=4.0):
        """优雅停机：server.shutdown() 必须跨线程调用；调度器一并停掉。"""
        with self._lock:
            try:
                import qoder_proxy
                server = qoder_proxy.SERVER
                if server is not None:
                    killer = threading.Thread(target=server.shutdown, daemon=True)
                    killer.start()
                    killer.join(timeout)
                sched = getattr(qoder_proxy, "SCHEDULER", None)
                if sched is not None:
                    try:
                        sched.stop()
                    except Exception:
                        pass
            except Exception:
                pass
            if self.thread:
                self.thread.join(timeout)
            self.thread = None

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())


def wait_healthy(port, timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if probe_gateway(port, timeout=1.5) == "ours":
            return True
        time.sleep(0.4)
    return False


def open_browser(port):
    def _open():
        try:
            webbrowser.open("http://127.0.0.1:%d/" % int(port))
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def dashboard_url(port):
    return "http://127.0.0.1:%d/" % int(port)


def open_data_dir(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def current_api_key(accounts_dir):
    try:
        import qoder_settings
        key, _ = qoder_settings.api_key_override(accounts_dir)
        return key
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 无 GUI 冒烟测试（CI 用）：起网关 → /health → /v1/models → / → 优雅停机
# ---------------------------------------------------------------------------
def pick_free_port(preferred):
    import socket
    for port in range(preferred, preferred + 16):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        if probe_gateway(port) is None:
            return port
    return preferred


def http_get(url, timeout=8.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def smoke_test(port):
    # 数据目录隔离：冻结产物按真实布局（便携目录）测；源码冒烟用临时目录，
    # 绝不碰仓库里的真实账号与配置。
    if is_frozen() and not (os.environ.get("QD_DATA_DIR") or "").strip():
        data = resolve_data_dir()
    else:
        import tempfile
        data = (os.environ.get("QD_DATA_DIR") or "").strip() \
            or tempfile.mkdtemp(prefix="qoder2api-smoke-")
        os.environ["QD_DATA_DIR"] = data
    paths = setup_environment(data)
    # --windowed 冻结产物里 print 无处可去：接管到日志文件，CI 靠日志/返回码判断。
    install_tee(queue.Queue(), os.path.join(paths["data"], LOG_NAME))
    gw = Gateway()
    gw.start("127.0.0.1", port, False)
    checks = [("health", wait_healthy(port, timeout=30.0))]
    if checks[0][1]:
        try:
            status, body = http_get("http://127.0.0.1:%d/v1/models" % port)
            checks.append(("models", status == 200 and '"data"' in body))
        except Exception:
            checks.append(("models", False))
        try:
            status, body = http_get("http://127.0.0.1:%d/" % port)
            checks.append(("dashboard", status == 200 and "Qoder" in body))
        except Exception:
            checks.append(("dashboard", False))
    gw.stop(timeout=6.0)
    lines = ["%s=%s" % (name, "OK" if good else "FAIL") for name, good in checks]
    result = "SMOKE " + ("OK" if all(good for _, good in checks) else "FAIL") \
        + "  " + "  ".join(lines)
    try:
        with open(os.path.join(paths["data"], "smoke-result.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(result + "\n")
    except OSError:
        pass
    print(result)
    return 0 if all(good for _, good in checks) else 1


# ---------------------------------------------------------------------------
# 通用启动/重启编排：所有 UI 共用
# ---------------------------------------------------------------------------
class ShellState(object):
    """内嵌壳共用状态机：starting → running / failed；供 worker 与 UI 解耦。"""

    def __init__(self, gw, log_queue):
        self.gw = gw
        self.events = queue.Queue()   # ("__EVENT__", name, payload)
        self.log_queue = log_queue    # Tee 的日志流（控制台壳展示用）
        self.stopping = False
        self.attached = False
        self.opened = False
        self.phase = "starting"

    def start_gateway(self, port, lan):
        gw = self.gw

        def worker():
            gw.stop(timeout=6.0)      # 重启路径下先确保旧实例完全停掉（幂等）
            gw.start("127.0.0.1", port, lan)
            ok = wait_healthy(port, timeout=30.0)
            note = None if ok else (gw.exit_note or "网关未能在 30 秒内就绪")
            self.events.put(("__EVENT__", "healthy" if ok else "failed", note))

        threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------------------
# UI 1：Windows —— tkinter 窗口 + WebView2 原生嵌入（自研 ctypes 宿主）
# ---------------------------------------------------------------------------
# 桌面桥：注入 window.__qdDesktop（看板据此显示「桌面客户端」设置区块，
# 并把开关变更经 chrome.webview.postMessage 回传给主进程）。
def desktop_bridge_js(cfg):
    return (r"(function(){var d=%s;"
            r"d.v=1;window.__qdDesktop=d;})()"
            % json.dumps({
                "tray": bool(cfg.get("tray", True)),
                "autostart": bool(cfg.get("autostart", False)),
                "autostart_supported": autostart_supported(),
            }, ensure_ascii=False))


def _print_window_shot(tk_root, png_path):
    """整窗截图（PrintWindow + PW_RENDERFULLCONTENT，含标题栏）→ PNG。

    诊断用：CapturePreview 只拍网页内容，看不出壳的顶栏；本函数拍整个
    窗口。锁屏/被遮挡时 PrintWindow 仍能取到离屏渲染内容。
    """
    import ctypes
    from ctypes import byref, c_int32, c_uint16, c_uint32, Structure, wintypes
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32

    class _BMIH(Structure):
        _fields_ = [("biSize", c_uint32), ("biWidth", c_int32),
                    ("biHeight", c_int32), ("biPlanes", c_uint16),
                    ("biBitCount", c_uint16), ("biCompression", c_uint32),
                    ("biSizeImage", c_uint32), ("biXPelsPerMeter", c_int32),
                    ("biYPelsPerMeter", c_int32), ("biClrUsed", c_uint32),
                    ("biClrImportant", c_uint32)]

    class _BMI(Structure):
        _fields_ = [("bmiHeader", _BMIH), ("bmiColors", c_uint32 * 3)]

    hwnd = u32.GetAncestor(tk_root.winfo_id(), 2)   # GA_ROOT
    rect = wintypes.RECT()
    u32.GetWindowRect(hwnd, byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hdc = u32.GetWindowDC(hwnd)
    mem = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    old = g32.SelectObject(mem, bmp)
    if not u32.PrintWindow(hwnd, mem, 2):            # PW_RENDERFULLCONTENT
        raise OSError("PrintWindow failed")
    # GetDIBits 要求位图未被选入任何 DC，先解除选中；须以 BITMAPINFO 结构
    # 按引用传参（实测裸数组传参会静默失败）。
    g32.SelectObject(mem, old)
    bi = _BMI()
    bi.bmiHeader = _BMIH(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)   # 负高=自上而下
    buf = ctypes.create_string_buffer(w * h * 4)
    if g32.GetDIBits(mem, bmp, 0, h, buf, byref(bi), 0) != h:
        raise OSError("GetDIBits failed")
    g32.DeleteObject(bmp)
    g32.DeleteDC(mem)
    u32.ReleaseDC(hwnd, hdc)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from make_icons import encode_png
    except Exception:
        encode_png = None
    raw = buf.raw
    if encode_png is None:
        with open(png_path, "wb") as fh:             # 兜底：直接落 BMP 通道序
            fh.write(raw)
        return
    rows = []
    for y in range(h):
        line = bytearray(raw[y * w * 4:(y + 1) * w * 4])
        for x in range(w):
            o = x * 4
            line[o], line[o + 2] = line[o + 2], line[o]   # BGRA → RGBA
            line[o + 3] = 255
        rows.append(line)
    with open(png_path, "wb") as fh:
        fh.write(encode_png(rows, w, h))


def run_windows_shell(state, cfg, config_path, paths, auto_close_ms=None,
                      hidden=False):
    import tkinter as tk
    from tkinter import ttk
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    if webview2_host is None:
        raise RuntimeError("webview2_host unavailable")

    root = tk.Tk()
    root.title("%s 控制台" % APP_TITLE)
    root.geometry("1180x780")
    root.minsize(820, 560)
    # 记忆窗口尺寸：恢复上次的宽高（钳制在最小尺寸与屏幕范围内）
    try:
        _w, _h = int(cfg.get("win_w") or 0), int(cfg.get("win_h") or 0)
        _sw, _sh = root.winfo_screenwidth(), root.winfo_screenheight()
        if 500 <= _w <= _sw and 400 <= _h <= _sh:
            root.geometry("%dx%d" % (_w, _h))
    except Exception:
        pass
    # 深色模式跟随系统：标题栏（DWM）+ 右键菜单配色；网页内容由
    # dashboard.html 的 prefers-color-scheme 媒体查询自动跟随。
    force_dark = (os.environ.get("QD_FORCE_DARK") or "").strip() \
        in ("1", "true", "yes")
    dark = force_dark or is_system_dark()
    force_light = (os.environ.get("QD_FORCE_LIGHT") or "").strip() \
        in ("1", "true", "yes")   # 验证用：强制 WebView2 浅色（Profile API）
    if sys.platform == "win32":
        try:
            import ctypes as _ct
            _apply_windows_titlebar_theme(
                _ct.windll.user32.GetAncestor(root.winfo_id(), 2), dark)
        except Exception:
            pass
    try:
        for icon in (os.path.join(bundle_dir(), "qoder2api.png"),
                     os.path.join(repo_dir(), "desktop", "assets",
                                  "qoder2api.png")):
            if os.path.isfile(icon):
                root.iconphoto(True, tk.PhotoImage(file=icon))
                break
    except Exception:
        pass

    # 无顶栏设计：状态进窗口标题、操作收进右键菜单，整窗只留内嵌看板。
    def set_status(text, color=GREEN):
        root.title("%s · %s" % (APP_TITLE, text))

    port_var = tk.StringVar(value=str(cfg["port"]))
    lan_var = tk.BooleanVar(value=cfg["lan"])

    def save_config_now():
        cfg["port"] = int(port_var.get())
        cfg["lan"] = bool(lan_var.get())
        save_config(config_path, cfg)

    def copy_api():
        root.clipboard_clear()
        root.clipboard_append(dashboard_url(port_var.get()).rstrip("/")
                              + "v1")
        set_status("接口地址已复制")

    def copy_key():
        key = current_api_key(paths["accounts"])
        if not key:
            set_status("当前未启用 API Key（本机模式无需密钥）")
            return
        root.clipboard_clear()
        root.clipboard_append(key)
        set_status("API Key 已复制（%s…%s）" % (key[:4], key[-4:]))

    def show_navigate_error(message):
        if host:
            host.navigate_to_string(error_html(message))

    def change_port():
        from tkinter import simpledialog
        raw = simpledialog.askstring(
            "修改端口", "网关端口（1024-65535，确定后自动重启）：",
            initialvalue=port_var.get(), parent=root)
        if raw is None:
            return
        port_var.set(raw.strip())
        apply_restart()

    def apply_restart():
        if state.stopping:
            return
        try:
            new_port = int(port_var.get())
            if not (1024 <= new_port <= 65535):
                raise ValueError
        except ValueError:
            set_status("端口无效：需要 1024-65535 的整数", color=RED)
            return
        save_config_now()
        state.stopping = True
        set_status("正在重启网关…", color=AMBER)

        def worker():
            state.gw.stop(timeout=6.0)
            state.stopping = False
            state.events.put(("__EVENT__", "restart-now", None))

        threading.Thread(target=worker, daemon=True).start()

    def open_settings():
        """跳转看板「设置」页（桌面客户端区块在其内）——不再单开设置窗口。"""
        show_main()
        try:
            host.execute_script("switchMainTab('settings')", lambda *a: None)
        except Exception:
            pass

    menu = tk.Menu(root, tearoff=0)
    if dark:
        menu.configure(bg="#1f2937", fg="#e5e7eb",
                       activebackground="#374151", activeforeground="#f9fafb")
    menu.add_command(label="应用并重启", command=apply_restart)
    menu.add_command(label="修改端口…", command=change_port)
    menu.add_command(label="设置…", command=open_settings)
    menu.add_checkbutton(label="允许局域网访问（重启生效）",
                         variable=lan_var, command=save_config_now)
    menu.add_separator()
    menu.add_command(label="复制接口地址", command=copy_api)
    menu.add_command(label="复制 API Key", command=copy_key)
    menu.add_command(label="打开数据目录",
                     command=lambda: open_data_dir(paths["data"]))
    menu.add_separator()
    menu.add_command(label="用外部浏览器打开看板",
                     command=lambda: open_browser(port_var.get()))

    def popup_menu(event):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    root.bind("<Button-3>", popup_menu)

    # -- 嵌入区 --------------------------------------------------------------
    web = tk.Frame(root, background="#f6f7f9")
    web.pack(side="top", fill="both", expand=True)
    root.update_idletasks()

    host = None
    embed_error = []

    def on_embed_error(message):
        embed_error.append(message)

    # 看板设置页开关的落盘队列：COM 回调只入队，drain 里再执行。
    pending_settings = queue.Queue()

    # 看板设置页开关 → 主进程。必须在 _on_webview_ready 之前定义（该回调在
    # host.start() 内同步触发，早于本函数下方的其余定义）。
    # ⚠️ 回调在 WebView2 的 COM 栈上执行：这里只做「入队」这件事本身（纯
    # Python，无 tkinter 调用），真正的应用交给 tk 线程的 drain 轮询——任何在
    # COM 回调栈上调 root.after/控件方法都会破坏本线程的 Python 线程状态
    # （实测 Fatal PyEval_RestoreThread: thread state is NULL，进程直接消失）。
    def on_web_message(payload):
        try:
            if isinstance(payload, dict) and payload.get("qd") == "set":
                pending_settings.put((payload.get("key"), payload.get("value")))
        except Exception as exc:
            print("[desktop] web message 处理失败: %r" % exc)

    try:
        host = webview2_host.WebView2Host(
            web.winfo_id(),
            os.path.join(paths["data"], "webview2-udf"),
            on_error=on_embed_error)

        def _on_webview_ready():
            # start() 是异步的，此刻 webview 指针才真正可用。
            # 滚动条隐藏/深色注入等一律在导航完成后走 execute_script，
            # 绝不在回调里直接调 AddScript（会崩，见 webview2_host 注释）。
            host.navigate_to_string(BOOT_HTML)
            try:
                host.on_web_message(on_web_message)
                print("[desktop] web message registered ok")
            except Exception as exc:
                print("[desktop] web message register failed: %r" % exc)
            if force_light:
                # Profile API 验证：强制内容浅色（默认 Auto 跟随系统）。
                # 经 root.after 回到事件循环再调，避免 COM 回调内重入。
                root.after(200, lambda: host.set_color_scheme(1))

        host._on_created = _on_webview_ready
        hr = host.start()
        if hr != 0:
            raise RuntimeError("WebView2 start hr=0x%08X" % (hr & 0xFFFFFFFF))
        web.bind("<Configure>", lambda _e: host and host.resize())
    except Exception as exc:
        host = None
        embed_error.append(str(exc))

    if host is None:
        # 内嵌不可用（缺 WebView2 运行时等）→ 退回控制台 UI + 浏览器。
        root.destroy()
        return run_console_shell(state, cfg, config_path, paths,
                                 fallback_note="WebView2 不可用：%s"
                                 % "; ".join(embed_error))

    # -- 事件编排 ------------------------------------------------------------
    def apply_desktop_setting(key, value):
        """看板「桌面客户端」开关 → 主进程执行（写注册表/摘挂托盘）。"""
        if key == "tray":
            cfg["tray"] = bool(value)
            save_config(config_path, cfg)
            if cfg["tray"] or hidden:
                ensure_tray()
            else:
                remove_tray()
            set_status("托盘已%s" % ("开启" if cfg["tray"] else "关闭"))
        elif key == "autostart":
            if autostart_supported() and autostart_apply(bool(value)):
                cfg["autostart"] = bool(value)
                save_config(config_path, cfg)
                set_status("开机自启已%s" % ("开启" if cfg["autostart"] else "关闭"))
            else:
                set_status("开机自启设置失败（仅安装版可用）", color=AMBER)
        if host:
            host.execute_script(desktop_bridge_js(cfg), lambda *a: None)

    def on_event(name, payload):
        if name == "healthy":
            state.phase = "running"
            set_status("网关运行中 · 端口 %s" % port_var.get())
            host.navigate(dashboard_url(port_var.get()))
            _inject_page_extras(root, host, cfg)
            # 渲染自证钩子（CI/诊断用）：QD_DESKTOP_SHOT=<png路径>
            shot = (os.environ.get("QD_DESKTOP_SHOT") or "").strip()
            if shot:
                def _shoot():
                    # QD_DESKTOP_LOGIN=1：先用默认密码 admin 自动登录并刷新，
                    # 截"完整看板"而非登录页（CI/诊断用；仅在全新数据目录安全）。
                    if (os.environ.get("QD_DESKTOP_LOGIN") or "").strip() \
                            in ("1", "true", "yes"):
                        def _after_reload():
                            # 页面重载后是新文档：桌面桥与（可选）深色调色板
                            # 都需要重新注入，否则设置区块会消失
                            host.execute_script(desktop_bridge_js(cfg),
                                                lambda *a: None)
                            if force_dark:
                                host.execute_script(DARK_VARS_JS, lambda *a: None)
                            def _snap():
                                try:
                                    print("[desktop] capture ->",
                                          host.capture(shot))
                                    wshot = (os.environ.get(
                                        "QD_DESKTOP_WINDOW_SHOT") or "").strip()
                                    if wshot:
                                        _print_window_shot(root, wshot)
                                        print("[desktop] window shot ->", wshot)
                                except Exception as exc:
                                    print("[desktop] shot failed: %r" % exc)
                            if (os.environ.get("QD_DESKTOP_SETTINGS")
                                    or "").strip() in ("1", "true", "yes"):
                                host.execute_script(
                                    "switchMainTab('settings');"
                                    "JSON.stringify({bridge: !!window.__qdDesktop,"
                                    "sec: (document.getElementById('desktopSection')||{}).style"
                                    " ? document.getElementById('desktopSection').style.display : 'NO-EL',"
                                    "tray: (document.getElementById('deskTray')||{}).checked})",
                                    lambda hr, res: print("[desktop] settings probe:", res))
                            if (os.environ.get("QD_DESKTOP_TOGGLE_TEST")
                                    or "").strip() in ("1", "true", "yes"):
                                # 端到端自检（会改配置，默认关闭）：模拟用户点开关，
                                # 验证 页面 → postMessage → 主进程落盘 整条链路。
                                def _toggle():
                                    host.execute_script(
                                        "var t=document.getElementById('deskTray');"
                                        "t.checked=!t.checked;desktopToggle('tray',t.checked);",
                                        lambda hr, res: print(
                                            "[desktop] toggle probe sent hr=0x%08X"
                                            % (hr & 0xFFFFFFFF)))
                                root.after(1500, _toggle)
                            root.after(1500, _snap)
                        host.execute_script(
                            "fetch('/panel/login',{method:'POST',"
                            "headers:{'Content-Type':'application/json'},"
                            "body:JSON.stringify({password:'admin'})})"
                            ".then(r=>r.json()).then(d=>{try{sessionStorage"
                            ".setItem(PANEL_STORE,d.token)}catch(e){};"
                            "location.reload();}).catch(()=>{})",
                            lambda hr, res: root.after(2500, _after_reload))
                        return
                    try:
                        ok = host.capture(shot)
                        print("[desktop] capture -> %s %s" % (ok, shot))
                    except Exception as exc:
                        print("[desktop] capture failed: %r" % exc)
                    # 滚动条隐藏自检：注入的 <style> 是否已在文档里
                    host.execute_script(
                        "String(!!document.getElementById('__qd_nosb'))",
                        lambda hr, res: print("[desktop] nosb style present:",
                                              res, "hr=0x%08X" % (hr & 0xFFFFFFFF)))
                    wshot = (os.environ.get("QD_DESKTOP_WINDOW_SHOT")
                             or "").strip()
                    if wshot:
                        try:
                            _print_window_shot(root, wshot)
                            print("[desktop] window shot ->", wshot)
                        except Exception as exc:
                            print("[desktop] window shot failed: %r" % exc)
                root.after(6000, _shoot)
        elif name == "failed":
            state.phase = "failed"
            note = payload or "网关未能启动"
            set_status(note, color=RED)
            show_navigate_error(note)
        elif name == "restart-now":
            state.phase = "starting"
            state.start_gateway(int(port_var.get()), bool(lan_var.get()))
            set_status("正在启动网关（端口 %s）…" % port_var.get(), color=AMBER)
        elif name == "tray-left":
            # 左键：隐藏中→显示；可见中→收进托盘
            if root.state() in ("withdrawn", "iconic"):
                show_main()
                print("[desktop] tray-left -> shown")
            else:
                root.withdraw()
                print("[desktop] tray-left -> hidden to tray")
        elif name == "tray-right" and tray:
            try:
                tray_menu.tk_popup(payload[0], payload[1])
            finally:
                tray_menu.grab_release()
        elif name == "closing":
            root.destroy()

    def drain():
        try:
            while True:
                item = state.events.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__EVENT__":
                    on_event(item[1], item[2])
        except queue.Empty:
            pass
        # 看板设置页开关：WebView2 回调只入队，真正干活（写盘/摘挂托盘/
        # 回注桥接）一律回到这里做——参见 on_web_message 的崩溃说明。
        while True:
            try:
                key, value = pending_settings.get_nowait()
            except queue.Empty:
                break
            try:
                apply_desktop_setting(key, value)
            except Exception as exc:
                print("[desktop] 桌面设置应用失败: %r" % exc)
        if (not state.stopping and not state.gw.running
                and state.phase == "running"):
            state.phase = "failed"
            note = state.gw.exit_note or "网关进程意外退出，详见日志"
            set_status(note, color=RED)
            show_navigate_error(note)
        root.after(250, drain)

    state.phase = "starting"
    pre = probe_gateway(cfg["port"])
    if pre == "ours":
        state.attached = True
        state.phase = "running"
        set_status("已连接到端口 %d 上运行的网关（附身模式）" % cfg["port"])
        host.navigate(dashboard_url(cfg["port"]))
        _inject_page_extras(root, host, cfg)
    elif pre == "foreign":
        state.phase = "failed"
        set_status("端口 %d 被其它程序占用。请换端口后点「应用并重启」"
                   % cfg["port"], color=RED)
        show_navigate_error("端口 %d 被其它程序占用。" % cfg["port"])
    else:
        set_status("正在启动网关（端口 %d）…" % cfg["port"], color=AMBER)
        state.start_gateway(cfg["port"], cfg["lan"])

    tray = None

    def ensure_tray():
        nonlocal tray
        if tray is None and sys.platform == "win32" and trayicon:
            tray = trayicon.TrayIcon(state.events)
            tray.start()

    def remove_tray():
        nonlocal tray
        if tray:
            tray.stop()
            tray = None

    def show_main():
        root.deiconify()
        root.lift()
        try:
            root.focus_force()
        except Exception:
            pass

    tray_menu = tk.Menu(root, tearoff=0)
    if dark:
        tray_menu.configure(bg="#1f2937", fg="#e5e7eb",
                            activebackground="#374151",
                            activeforeground="#f9fafb")
    tray_menu.add_command(label="显示主窗口", command=show_main)
    tray_menu.add_command(label="打开看板",
                          command=lambda: open_browser(port_var.get()))
    tray_menu.add_command(label="设置…", command=open_settings)
    tray_menu.add_separator()
    tray_menu.add_command(label="退出", command=lambda: real_exit())

    # 托盘：默认开启；--hidden（开机自启）强制开启并直接藏进托盘
    if cfg.get("tray", True) or hidden:
        ensure_tray()
    if hidden:
        root.after(80, root.withdraw)
    if (os.environ.get("QD_TRAY_TEST") or "").strip() == "1":
        # 诊断：7s 收进托盘 → 9.5s 再显示（验证托盘事件链路）
        root.after(7000, lambda: state.events.put(
            ("__EVENT__", "tray-left", None)))
        root.after(9500, lambda: state.events.put(
            ("__EVENT__", "tray-left", None)))

    def real_exit():
        # 托盘菜单「退出」/ 关窗即退模式下的唯一真退出路径：
        # 记忆窗口尺寸 → 摘托盘 → 优雅停机（6s 兜底强杀，绝不留残留进程）
        try:
            if root.state() == "normal":
                cfg["win_w"] = root.winfo_width()
                cfg["win_h"] = root.winfo_height()
                save_config(config_path, cfg)
        except Exception:
            pass
        remove_tray()
        if state.stopping:
            root.destroy()
            return
        state.stopping = True
        set_status("正在停止网关…", color=AMBER)
        root.after(6000, lambda: os._exit(0))

        def worker():
            if not state.attached:
                state.gw.stop(timeout=4.0)
            state.events.put(("__EVENT__", "closing", None))

        threading.Thread(target=worker, daemon=True).start()

    def on_close():
        if tray and cfg.get("tray", True):
            root.withdraw()   # 收进托盘；真正退出走托盘菜单「退出」
            return
        real_exit()

    def on_unmap(_event):
        # 最小化（iconic）后收进托盘；withdraw 自身的 Unmap 不响应
        if tray and cfg.get("tray", True) and root.state() == "iconic":
            root.after(200, lambda: root.state() == "iconic" and root.withdraw())

    root.bind("<Unmap>", on_unmap)

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(250, drain)
    if auto_close_ms:
        # 冒烟自检必须走真退出（托盘模式下 on_close 只是收进托盘）
        root.after(auto_close_ms, real_exit)
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# UI 2：macOS —— pywebview（WKWebView）
# ---------------------------------------------------------------------------
def _inject_scrollbar_css(window):
    """pywebview 壳的滚动条隐藏：load 完成后注入 CSS（无初始脚本机制）。"""
    def _run():
        time.sleep(1.5)
        try:
            window.evaluate_js(HIDE_SCROLLBAR_JS)
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


def run_mac_shell(state, cfg, config_path, paths, auto_close_ms=None):
    if _pywebview is None:
        raise RuntimeError("pywebview unavailable")
    webview = _pywebview
    window = webview.create_window(
        APP_TITLE, html=BOOT_HTML, width=1180, height=800,
        min_size=(820, 560), js_api=_MacBridge(state, cfg, config_path, paths))
    save_config(config_path, cfg)

    def bootstrap():
        pre = probe_gateway(cfg["port"])
        if pre == "ours":
            state.attached = True
            window.load_url(dashboard_url(cfg["port"]))
            _inject_scrollbar_css(window)
            return
        if pre == "foreign":
            window.load_html(error_html("端口 %d 被其它程序占用。" % cfg["port"],
                                        with_retry_js=True))
            return
        state.start_gateway(cfg["port"], cfg["lan"])
        try:
            name, payload = (state.events.get(timeout=45))[1:]
        except queue.Empty:
            name, payload = "failed", "网关启动超时"
        if name == "healthy":
            window.load_url(dashboard_url(cfg["port"]))
            _inject_scrollbar_css(window)
        else:
            window.load_html(error_html(payload or "网关未能启动",
                                        with_retry_js=True))

    if auto_close_ms:
        def auto_close():
            time.sleep(auto_close_ms / 1000.0)
            try:
                window.destroy()
            except Exception:
                pass
        threading.Thread(target=auto_close, daemon=True).start()

    webview.start(bootstrap)      # 阻塞到窗口关闭；bootstrap 在后台线程跑
    if not state.attached:
        state.gw.stop(timeout=4.0)
    return 0


class _MacBridge(object):
    """pywebview js_api：错误页「重试」与常用动作。"""

    def __init__(self, state, cfg, config_path, paths):
        self.state = state
        self.cfg = cfg
        self.config_path = config_path
        self.paths = paths

    def retry(self):
        state, cfg = self.state, self.cfg
        state.stopping = True

        def worker():
            import webview as wv
            state.gw.stop(timeout=6.0)
            state.stopping = False
            state.start_gateway(cfg["port"], cfg["lan"])
            ok = wait_healthy(cfg["port"], timeout=30.0)
            target = next(iter(getattr(wv, "windows", [])), None)
            if target is None:
                return
            if ok:
                target.load_url(dashboard_url(cfg["port"]))
                _inject_scrollbar_css(target)
            else:
                target.load_html(error_html("网关未能启动（端口 %d）。"
                                            % cfg["port"], with_retry_js=True))

        threading.Thread(target=worker, daemon=True).start()

    def open_data_dir(self):
        open_data_dir(self.paths["data"])

    def open_external(self):
        open_browser(self.cfg["port"])


# ---------------------------------------------------------------------------
# UI 3：Linux —— PySide6 + QtWebEngine
# ---------------------------------------------------------------------------
def run_linux_shell(state, cfg, config_path, paths, auto_close_ms=None):
    if QWebEngineView is None:
        raise RuntimeError("PySide6/QtWebEngine unavailable")
    from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import (QApplication, QInputDialog, QMainWindow,
                                   QMenu)

    app = QApplication.instance() or QApplication(sys.argv)
    try:
        app.setWindowIcon(QIcon(os.path.join(bundle_dir(), "qoder2api.png")))
    except Exception:
        pass

    # Qt 信号必须挂在 QObject 上：用一个小载体做 worker → UI 线程的事件桥。
    class _Bus(QObject):
        fired = Signal(str, object)

    # closeEvent 是 C++ 虚函数，实例属性覆盖无效，必须子类化。
    close_hook = {"fn": lambda ev: ev.accept()}

    class _Window(QMainWindow):
        def closeEvent(self, ev):
            close_hook["fn"](ev)

    bus = _Bus()
    win = _Window()
    win.resize(1180, 800)
    win.setMinimumSize(820, 560)

    # 无顶栏设计：状态进窗口标题、操作收进右键菜单，整窗只留内嵌看板。
    def set_status(text, color=GREEN):
        win.setWindowTitle("%s · %s" % (APP_TITLE, text))

    port_holder = {"v": int(cfg["port"])}
    lan_holder = {"v": bool(cfg["lan"])}

    def save_config_now():
        cfg["port"] = int(port_holder["v"])
        cfg["lan"] = bool(lan_holder["v"])
        save_config(config_path, cfg)

    def copy_api():
        app.clipboard().setText(
            dashboard_url(port_holder["v"]).rstrip("/") + "v1")
        set_status("接口地址已复制")

    def copy_key():
        key = current_api_key(paths["accounts"])
        if not key:
            set_status("当前未启用 API Key（本机模式无需密钥）")
            return
        app.clipboard().setText(key)
        set_status("API Key 已复制（%s…%s）" % (key[:4], key[-4:]))

    def show_error(message):
        view.setHtml(error_html(message))

    def change_port():
        raw, ok = QInputDialog.getInt(
            win, "修改端口", "网关端口（1024-65535，确定后自动重启）：",
            port_holder["v"], 1024, 65535)
        if not ok:
            return
        port_holder["v"] = int(raw)
        apply_restart()

    def apply_restart():
        if state.stopping:
            return
        save_config_now()
        state.stopping = True
        set_status("正在重启网关…", color=AMBER)

        def worker():
            state.gw.stop(timeout=6.0)
            state.stopping = False
            bus.fired.emit("restart-now", None)

        threading.Thread(target=worker, daemon=True).start()

    view = QWebEngineView()
    win.setCentralWidget(view)
    view.setHtml(BOOT_HTML)
    # 隐藏默认滚动条：每个文档加载完注入 CSS（外观隐藏，滚轮滚动不受影响）。
    view.page().loadFinished.connect(
        lambda _ok: view.page().runJavaScript(HIDE_SCROLLBAR_JS))

    def popup_menu(pos):
        menu = QMenu(win)
        menu.addAction("应用并重启", apply_restart)
        menu.addAction("修改端口…", change_port)
        act_lan = menu.addAction("允许局域网访问（重启生效）")
        act_lan.setCheckable(True)
        act_lan.setChecked(lan_holder["v"])
        act_lan.toggled.connect(lambda on: (lan_holder.__setitem__("v", on),
                                            save_config_now()))
        menu.addSeparator()
        menu.addAction("复制接口地址", copy_api)
        menu.addAction("复制 API Key", copy_key)
        menu.addAction("打开数据目录", lambda: open_data_dir(paths["data"]))
        menu.addSeparator()
        menu.addAction("用外部浏览器打开看板",
                       lambda: open_browser(port_holder["v"]))
        menu.exec(view.mapToGlobal(pos))

    view.setContextMenuPolicy(Qt.CustomContextMenu)
    view.customContextMenuRequested.connect(popup_menu)

    def on_event(name, payload):
        if name == "healthy":
            state.phase = "running"
            set_status("网关运行中 · 端口 %d" % port_holder["v"])
            view.load(QUrl(dashboard_url(port_holder["v"])))
        elif name == "failed":
            state.phase = "failed"
            note = payload or "网关未能启动"
            set_status(note, color=RED)
            show_error(note)
        elif name == "restart-now":
            state.phase = "starting"
            set_status("正在启动网关（端口 %d）…" % port_holder["v"],
                       color=AMBER)
            state.start_gateway(port_holder["v"], lan_holder["v"])
        elif name == "closing":
            win.close()

    bus.fired.connect(on_event)

    def poll_events():
        try:
            while True:
                item = state.events.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__EVENT__":
                    on_event(item[1], item[2])
        except queue.Empty:
            pass
        if (not state.stopping and not state.gw.running
                and state.phase == "running"):
            state.phase = "failed"
            note = state.gw.exit_note or "网关进程意外退出，详见日志"
            set_status(note, color=RED)
            show_error(note)

    timer = QTimer()
    timer.timeout.connect(poll_events)
    timer.start(250)

    state.phase = "starting"
    pre = probe_gateway(cfg["port"])
    if pre == "ours":
        state.attached = True
        state.phase = "running"
        set_status("已连接到端口 %d 上运行的网关（附身模式）" % cfg["port"])
        view.load(QUrl(dashboard_url(cfg["port"])))
    elif pre == "foreign":
        state.phase = "failed"
        set_status("端口 %d 被其它程序占用。请换端口后点「应用并重启」"
                   % cfg["port"], color=RED)
        show_error("端口 %d 被其它程序占用。" % cfg["port"])
    else:
        set_status("正在启动网关（端口 %d）…" % cfg["port"], color=AMBER)
        state.start_gateway(cfg["port"], cfg["lan"])

    closing = {"done": False}

    def real_close():
        if closing["done"]:
            return
        closing["done"] = True
        QTimer.singleShot(5000, lambda: os._exit(0))   # 兜底：不留残留进程

        def worker():
            if not state.attached:
                state.gw.stop(timeout=4.0)
            os._exit(0)

        threading.Thread(target=worker, daemon=True).start()

    def on_close(ev):
        ev.ignore()                      # 等优雅停机完成（os._exit 收尾）
        if state.stopping:
            return
        state.stopping = True
        set_status("正在停止网关…", color=AMBER)
        real_close()

    close_hook["fn"] = on_close

    if auto_close_ms:
        QTimer.singleShot(auto_close_ms, lambda: win.close())
    win.show()
    app.exec()
    return 0


# ---------------------------------------------------------------------------
# UI 4（兜底）：tkinter 控制台 + 浏览器打开看板
# ---------------------------------------------------------------------------
def run_console_shell(state, cfg, config_path, paths, auto_close_ms=None,
                      fallback_note=None):
    import tkinter as tk
    from tkinter import ttk
    from tkinter.scrolledtext import ScrolledText

    log_queue = state.log_queue
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    root.title("%s 控制台（看板将在浏览器打开）" % APP_TITLE)
    root.minsize(680, 480)
    try:
        for icon in (os.path.join(bundle_dir(), "qoder2api.png"),
                     os.path.join(repo_dir(), "desktop", "assets",
                                  "qoder2api.png")):
            if os.path.isfile(icon):
                root.iconphoto(True, tk.PhotoImage(file=icon))
                break
    except Exception:
        pass

    status_var = tk.StringVar(value="启动中…")
    status_label = tk.Label(root, textvariable=status_var, anchor="w",
                            font=("TkDefaultFont", 11, "bold"))
    status_label.grid(row=0, column=0, sticky="ew", padx=10, pady=6)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(4, weight=1)

    def set_status(text, color=GREEN):
        status_var.set(text)
        status_label.config(fg=color)

    api_var = tk.StringVar(value="http://127.0.0.1:%d/v1" % cfg["port"])
    api_entry = ttk.Entry(root, textvariable=api_var, state="readonly")
    api_entry.grid(row=1, column=0, sticky="ew", padx=10)

    def refresh_address():
        api_var.set("http://127.0.0.1:%s/v1" % port_var.get())

    def copy_api():
        root.clipboard_clear()
        root.clipboard_append(api_var.get())
        set_status("API 地址已复制")

    ctrl = ttk.Frame(root)
    ctrl.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
    port_var = tk.StringVar(value=str(cfg["port"]))
    lan_var = tk.BooleanVar(value=cfg["lan"])
    autoopen_var = tk.BooleanVar(value=cfg["auto_open"])
    port_var.trace_add("write", lambda *_: refresh_address())

    def save_config_now():
        cfg["port"] = int(port_var.get())
        cfg["lan"] = bool(lan_var.get())
        cfg["auto_open"] = bool(autoopen_var.get())
        save_config(config_path, cfg)

    def apply_restart():
        if state.stopping:
            return
        try:
            new_port = int(port_var.get())
            if not (1024 <= new_port <= 65535):
                raise ValueError
        except ValueError:
            set_status("端口无效：需要 1024-65535 的整数", color=RED)
            return
        save_config_now()
        state.stopping = True
        restart_btn.state(["disabled"])
        set_status("正在重启网关…", color=AMBER)

        def worker():
            state.gw.stop(timeout=6.0)
            state.stopping = False
            state.events.put(("__EVENT__", "restart-now", None))

        threading.Thread(target=worker, daemon=True).start()

    restart_btn = ttk.Button(ctrl, text="应用并重启", command=apply_restart)
    ttk.Label(ctrl, text="端口").grid(row=0, column=0)
    ttk.Spinbox(ctrl, from_=1024, to=65535, textvariable=port_var, width=7)\
        .grid(row=0, column=1, padx=(4, 10))
    ttk.Checkbutton(ctrl, text="允许局域网访问（自动生成随机 Key）",
                    variable=lan_var).grid(row=0, column=2)
    ttk.Checkbutton(ctrl, text="自动打开看板",
                    variable=autoopen_var).grid(row=0, column=3, padx=(10, 0))
    restart_btn.grid(row=0, column=4, padx=(10, 0))

    btn = ttk.Frame(root)
    btn.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 6))
    ttk.Button(btn, text="打开看板（浏览器）",
               command=lambda: open_browser(port_var.get())).pack(side="left")

    def copy_key():
        key = current_api_key(paths["accounts"])
        if not key:
            set_status("当前未启用 API Key（本机模式无需密钥）", color=AMBER)
            return
        root.clipboard_clear()
        root.clipboard_append(key)
        set_status("API Key 已复制（%s…%s）" % (key[:4], key[-4:]))

    ttk.Button(btn, text="复制 API Key", command=copy_key)\
        .pack(side="left", padx=(8, 0))
    ttk.Button(btn, text="打开数据目录",
               command=lambda: open_data_dir(paths["data"]))\
        .pack(side="left", padx=(8, 0))

    log_box = ScrolledText(root, height=12, wrap="word", state="disabled",
                           font=("TkFixedFont", 9))
    log_box.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 10))

    def on_event(name, payload):
        if name == "healthy":
            state.phase = "running"
            restart_btn.state(["!disabled"])
            set_status("网关运行中 · http://127.0.0.1:%s/" % port_var.get())
            if autoopen_var.get() and not state.opened:
                state.opened = True
                open_browser(port_var.get())
        elif name == "failed":
            state.phase = "failed"
            restart_btn.state(["!disabled"])
            set_status(payload or "网关未能启动", color=RED)
        elif name == "restart-now":
            state.phase = "starting"
            set_status("正在启动网关（端口 %s）…" % port_var.get(), color=AMBER)
            restart_btn.state(["disabled"])
            state.start_gateway(int(port_var.get()), bool(lan_var.get()))
        elif name == "closing":
            root.destroy()

    def drain():
        try:
            while True:
                item = state.events.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "__EVENT__":
                    on_event(item[1], item[2])
                    continue
                log_box.config(state="normal")
                log_box.insert("end", item)
                if int(log_box.index("end-1c").split(".")[0]) > 800:
                    log_box.delete("1.0", "200.0")
                log_box.see("end")
                log_box.config(state="disabled")
        except queue.Empty:
            pass
        if (state.phase == "running" and not state.stopping
                and not state.gw.running):
            state.phase = "failed"
            restart_btn.state(["!disabled"])
            set_status(state.gw.exit_note or "网关进程意外退出，详见日志",
                       color=RED)
        root.after(250, drain)

    def on_close():
        if state.stopping:
            root.destroy()
            return
        state.stopping = True
        set_status("正在停止网关…", color=AMBER)
        root.after(6000, lambda: os._exit(0))

        def worker():
            if not state.attached:
                state.gw.stop(timeout=4.0)
            state.events.put(("__EVENT__", "closing", None))

        threading.Thread(target=worker, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", on_close)

    state.phase = "starting"
    pre = probe_gateway(cfg["port"])
    if pre == "ours":
        state.attached = True
        state.phase = "running"
        set_status("已有 Qoder2API 实例在端口 %d 运行，本窗口未重复启动"
                   % cfg["port"])
    elif pre == "foreign":
        state.phase = "failed"
        set_status("端口 %d 被其它程序占用。请换一个端口后点「应用并重启」"
                   % cfg["port"], color=RED)
    else:
        set_status("正在启动网关（端口 %d）…" % cfg["port"], color=AMBER)
        restart_btn.state(["disabled"])
        state.start_gateway(cfg["port"], cfg["lan"])

    if fallback_note:
        set_status("内嵌看板不可用，已回退控制台（%s）" % fallback_note,
                   color=AMBER)

    root.after(250, drain)
    if auto_close_ms:
        root.after(auto_close_ms, on_close)
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# UI 选择与降级链
# ---------------------------------------------------------------------------
def preferred_shell():
    if sys.platform == "win32" and webview2_host is not None:
        return run_windows_shell
    if sys.platform == "darwin" and _pywebview is not None:
        return run_mac_shell
    if sys.platform.startswith("linux") and QWebEngineView is not None:
        return run_linux_shell
    return None


def run_gui(port, lan, auto_open, auto_close_ms=None, force_console=False,
            hidden=False):
    """打开控制台窗口并阻塞到关闭。auto_close_ms 用于 GUI 冒烟自检；
    hidden=True 时启动即进托盘（开机自启用）。"""
    paths = setup_environment(resolve_data_dir())
    config_path = os.path.join(paths["data"], CONFIG_NAME)
    cfg = load_config(config_path)
    if autostart_supported():
        # 配置里的 autostart 以系统实际状态为准（外部手动删了注册表也要反映）
        cfg["autostart"] = autostart_enabled()
    log_queue = queue.Queue()
    install_tee(log_queue, os.path.join(paths["data"], LOG_NAME))
    state = ShellState(Gateway(), log_queue)

    shells = []
    if not force_console:
        shell = preferred_shell()
        if shell is not None:
            if shell is run_windows_shell:
                shells.append(functools.partial(shell, hidden=hidden))
            else:
                shells.append(shell)
    shells.append(run_console_shell)

    last_error = None
    for shell in shells:
        try:
            return shell(state, cfg, config_path, paths,
                         auto_close_ms=auto_close_ms)
        except Exception:
            last_error = traceback.format_exc()
            traceback.print_exc()
    raise RuntimeError("no usable UI shell; last error:\n%s" % last_error)


def parse_args(argv):
    opts = {"port": DEFAULT_PORT, "lan": False, "auto_open": True,
            "mode": "gui", "data_dir": None, "auto_close_ms": None,
            "hidden": False, "force_console": False}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--port":
            i += 1
            opts["port"] = int(argv[i])
        elif arg == "--lan":
            opts["lan"] = True
        elif arg == "--no-open":
            opts["auto_open"] = False
        elif arg == "--hidden":
            opts["hidden"] = True
        elif arg == "--ui" and i + 1 < len(argv):
            i += 1
            opts["force_console"] = argv[i] == "console"
        elif arg == "--console-ui":
            opts["force_console"] = True
        elif arg == "--smoke-test":
            opts["mode"] = "smoke"
        elif arg == "--smoke-gui":
            opts["mode"] = "smoke-gui"
        elif arg == "--auto-close-ms":
            i += 1
            opts["auto_close_ms"] = int(argv[i])
        elif arg == "--data-dir":
            i += 1
            opts["data_dir"] = argv[i]
        elif arg in ("-h", "--help"):
            opts["mode"] = "help"
        i += 1
    return opts


def main(argv=None):
    opts = parse_args(list(sys.argv[1:] if argv is None else argv))
    if opts["mode"] == "help":
        print(__doc__)
        return 0
    if opts["data_dir"]:
        os.environ["QD_DATA_DIR"] = opts["data_dir"]
    if opts["mode"] == "smoke":
        return smoke_test(pick_free_port(opts["port"]))
    if opts["mode"] == "smoke-gui":
        # GUI 自检：开窗 → 自动触发真实关闭流程（优雅停机）→ 返回码给 CI。
        return run_gui(opts["port"], opts["lan"], False,
                       auto_close_ms=opts["auto_close_ms"] or 6000,
                       force_console=opts["force_console"])
    return run_gui(opts["port"], opts["lan"], opts["auto_open"],
                   force_console=opts["force_console"],
                   hidden=opts["hidden"])


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if not is_frozen():
        # 源码运行：desktop/ 不是仓库根，把仓库根加进 sys.path 才能找到网关模块。
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _root not in sys.path:
            sys.path.insert(0, _root)
    sys.exit(main())
