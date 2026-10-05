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
  <div class="err">%s<br>可在窗口工具栏修改端口后点「应用并重启」。</div>%s
""" % (message, button))


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


def load_config(config_path, port, lan, auto_open):
    try:
        with open(config_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        cfg = {}
    return {"port": int(cfg.get("port") or port),
            "lan": bool(cfg.get("lan", lan)),
            "auto_open": bool(cfg.get("auto_open", auto_open))}


def save_config(config_path, port, lan, auto_open):
    try:
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump({"port": int(port), "lan": bool(lan),
                       "auto_open": bool(auto_open)}, fh)
    except (OSError, ValueError):
        pass


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
def run_windows_shell(state, cfg, config_path, paths, auto_close_ms=None):
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
    try:
        for icon in (os.path.join(bundle_dir(), "qoder2api.png"),
                     os.path.join(repo_dir(), "desktop", "assets",
                                  "qoder2api.png")):
            if os.path.isfile(icon):
                root.iconphoto(True, tk.PhotoImage(file=icon))
                break
    except Exception:
        pass

    status_var = tk.StringVar(value="正在启动…")

    def set_status(text, color=GREEN):
        status_var.set(text)
        status_label.config(fg=color)

    # -- 顶部工具条 ----------------------------------------------------------
    top = tk.Frame(root)
    top.pack(side="top", fill="x", padx=8, pady=(8, 4))
    status_label = tk.Label(top, textvariable=status_var, anchor="w",
                            font=("TkDefaultFont", 10, "bold"))
    status_label.pack(side="left", padx=(2, 10))

    port_var = tk.StringVar(value=str(cfg["port"]))
    lan_var = tk.BooleanVar(value=cfg["lan"])

    def save_config_now():
        save_config(config_path, port_var.get(), lan_var.get(),
                    autoopen_var.get())

    def copy_api():
        root.clipboard_clear()
        root.clipboard_append(dashboard_url(port_var.get()).rstrip("/")
                              + "v1")
        set_status("接口地址已复制")

    def copy_key():
        key = current_api_key(paths["accounts"])
        if not key:
            set_status("当前未启用 API Key（本机模式无需密钥）", color=AMBER)
            return
        root.clipboard_clear()
        root.clipboard_append(key)
        set_status("API Key 已复制（%s…%s）" % (key[:4], key[-4:]))

    def show_navigate_error(message):
        if host:
            host.navigate_to_string(error_html(message))

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

    restart_btn = ttk.Button(top, text="应用并重启", command=apply_restart)
    ttk.Label(top, text="端口").pack(side="left")
    ttk.Spinbox(top, from_=1024, to=65535, textvariable=port_var, width=7)\
        .pack(side="left", padx=(4, 10))
    ttk.Checkbutton(top, text="局域网", variable=lan_var).pack(side="left")
    restart_btn.pack(side="left", padx=(10, 0))
    ttk.Button(top, text="外部浏览器", command=lambda: open_browser(
        port_var.get())).pack(side="left", padx=(10, 0))
    ttk.Button(top, text="复制接口", command=copy_api).pack(side="left",
                                                           padx=(6, 0))
    ttk.Button(top, text="复制Key", command=copy_key).pack(side="left",
                                                           padx=(6, 0))
    ttk.Button(top, text="数据目录", command=lambda: open_data_dir(
        paths["data"])).pack(side="left", padx=(6, 0))

    # -- 嵌入区 --------------------------------------------------------------
    web = tk.Frame(root, background="#f6f7f9")
    web.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 8))
    root.update_idletasks()

    host = None
    embed_error = []

    def on_embed_error(message):
        embed_error.append(message)

    try:
        host = webview2_host.WebView2Host(
            web.winfo_id(),
            os.path.join(paths["data"], "webview2-udf"),
            on_error=on_embed_error)
        host._on_created = lambda: None
        hr = host.start()
        if hr != 0:
            raise RuntimeError("WebView2 start hr=0x%08X" % (hr & 0xFFFFFFFF))
        host.navigate_to_string(BOOT_HTML)
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
    def on_event(name, payload):
        if name == "healthy":
            state.phase = "running"
            set_status("网关运行中 · 端口 %s" % port_var.get())
            restart_btn.state(["!disabled"])
            host.navigate(dashboard_url(port_var.get()))
            # 渲染自证钩子（CI/诊断用）：QD_DESKTOP_SHOT=<png路径>
            shot = (os.environ.get("QD_DESKTOP_SHOT") or "").strip()
            if shot:
                def _shoot():
                    try:
                        ok = host.capture(shot)
                        print("[desktop] capture -> %s %s" % (ok, shot))
                    except Exception as exc:
                        print("[desktop] capture failed: %r" % exc)
                root.after(5000, _shoot)
        elif name == "failed":
            state.phase = "failed"
            restart_btn.state(["!disabled"])
            note = payload or "网关未能启动"
            set_status(note, color=RED)
            show_navigate_error(note)
        elif name == "restart-now":
            state.phase = "starting"
            state.start_gateway(int(port_var.get()), bool(lan_var.get()))
            set_status("正在启动网关（端口 %s）…" % port_var.get(), color=AMBER)
            restart_btn.state(["disabled"])
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
    elif pre == "foreign":
        state.phase = "failed"
        set_status("端口 %d 被其它程序占用。请换端口后点「应用并重启」"
                   % cfg["port"], color=RED)
        show_navigate_error("端口 %d 被其它程序占用。" % cfg["port"])
    else:
        set_status("正在启动网关（端口 %d）…" % cfg["port"], color=AMBER)
        restart_btn.state(["disabled"])
        state.start_gateway(cfg["port"], cfg["lan"])

    def on_close():
        if state.stopping:
            root.destroy()
            return
        state.stopping = True
        set_status("正在停止网关…", color=AMBER)
        root.after(6000, lambda: os._exit(0))   # 兜底：绝不留残留进程

        def worker():
            if not state.attached:
                state.gw.stop(timeout=4.0)
            state.events.put(("__EVENT__", "closing", None))

        threading.Thread(target=worker, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(250, drain)
    if auto_close_ms:
        root.after(auto_close_ms, on_close)
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# UI 2：macOS —— pywebview（WKWebView）
# ---------------------------------------------------------------------------
def run_mac_shell(state, cfg, config_path, paths, auto_close_ms=None):
    if _pywebview is None:
        raise RuntimeError("pywebview unavailable")
    webview = _pywebview
    window = webview.create_window(
        APP_TITLE, html=BOOT_HTML, width=1180, height=800,
        min_size=(820, 560), js_api=_MacBridge(state, cfg, config_path, paths))
    save_config(config_path, cfg["port"], cfg["lan"], cfg["auto_open"])

    def bootstrap():
        pre = probe_gateway(cfg["port"])
        if pre == "ours":
            state.attached = True
            window.load_url(dashboard_url(cfg["port"]))
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
    from PySide6.QtCore import QObject, QTimer, QUrl, Signal
    from PySide6.QtGui import QAction, QIcon
    from PySide6.QtWidgets import (QApplication, QCheckBox, QLabel, QMainWindow,
                                   QSpinBox, QToolBar)

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
    win.setWindowTitle("%s 控制台" % APP_TITLE)
    win.resize(1180, 800)
    win.setMinimumSize(820, 560)

    bar = QToolBar()
    bar.setMovable(False)
    win.addToolBar(bar)
    status_lbl = QLabel("正在启动…")
    status_lbl.setStyleSheet("padding:0 10px; font-weight:bold;")
    bar.addWidget(status_lbl)
    bar.addSeparator()
    port_spin = QSpinBox()
    port_spin.setRange(1024, 65535)
    port_spin.setValue(int(cfg["port"]))
    bar.addWidget(QLabel(" 端口 "))
    bar.addWidget(port_spin)
    lan_box = QCheckBox("局域网")
    lan_box.setChecked(cfg["lan"])
    bar.addWidget(lan_box)

    def set_status(text, color=GREEN):
        status_lbl.setText(text)
        status_lbl.setStyleSheet(
            "padding:0 10px; font-weight:bold; color:%s;" % color)

    view = QWebEngineView()
    win.setCentralWidget(view)
    view.setHtml(BOOT_HTML)

    def copy_api():
        app.clipboard().setText(
            dashboard_url(port_spin.value()).rstrip("/") + "v1")
        set_status("接口地址已复制")

    def copy_key():
        key = current_api_key(paths["accounts"])
        if not key:
            set_status("当前未启用 API Key（本机模式无需密钥）", color=AMBER)
            return
        app.clipboard().setText(key)
        set_status("API Key 已复制（%s…%s）" % (key[:4], key[-4:]))

    def show_error(message):
        view.setHtml(error_html(message))

    def apply_restart():
        if state.stopping:
            return
        save_config(config_path, port_spin.value(), lan_box.isChecked(),
                    cfg["auto_open"])
        state.stopping = True
        set_status("正在重启网关…", color=AMBER)

        def worker():
            state.gw.stop(timeout=6.0)
            state.stopping = False
            bus.fired.emit("restart-now", None)

        threading.Thread(target=worker, daemon=True).start()

    from PySide6.QtGui import QAction
    act_restart = QAction("应用并重启", win)
    act_restart.triggered.connect(apply_restart)
    bar.addSeparator()
    bar.addAction(act_restart)
    act_ext = QAction("外部浏览器", win)
    act_ext.triggered.connect(lambda: open_browser(port_spin.value()))
    bar.addAction(act_ext)
    act_api = QAction("复制接口", win)
    act_api.triggered.connect(copy_api)
    bar.addAction(act_api)
    act_key = QAction("复制Key", win)
    act_key.triggered.connect(copy_key)
    bar.addAction(act_key)
    act_dir = QAction("数据目录", win)
    act_dir.triggered.connect(lambda: open_data_dir(paths["data"]))
    bar.addAction(act_dir)

    def on_event(name, payload):
        if name == "healthy":
            state.phase = "running"
            set_status("网关运行中 · 端口 %d" % port_spin.value())
            view.load(QUrl(dashboard_url(port_spin.value())))
        elif name == "failed":
            state.phase = "failed"
            note = payload or "网关未能启动"
            set_status(note, color=RED)
            show_error(note)
        elif name == "restart-now":
            state.stopping = False
            state.phase = "starting"
            set_status("正在启动网关（端口 %d）…" % port_spin.value(),
                       color=AMBER)
            state.start_gateway(port_spin.value(), lan_box.isChecked())
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
        save_config(config_path, port_var.get(), lan_var.get(),
                    autoopen_var.get())

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


def run_gui(port, lan, auto_open, auto_close_ms=None, force_console=False):
    """打开控制台窗口并阻塞到关闭。auto_close_ms 用于 GUI 冒烟自检。"""
    paths = setup_environment(resolve_data_dir())
    config_path = os.path.join(paths["data"], CONFIG_NAME)
    cfg = load_config(config_path, port, lan, auto_open)
    log_queue = queue.Queue()
    install_tee(log_queue, os.path.join(paths["data"], LOG_NAME))
    state = ShellState(Gateway(), log_queue)

    shells = []
    if not force_console:
        shell = preferred_shell()
        if shell is not None:
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
            "force_console": False}
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
                   force_console=opts["force_console"])


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
