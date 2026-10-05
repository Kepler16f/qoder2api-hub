#!/usr/bin/env python3
"""_test_webview2.py —— WebView2 COM 宿主真机测试（自动开窗、8 秒后自动关闭）。

    python desktop/_test_webview2.py

预期：弹出 900x600 窗口，白色页面渲染「WebView2 OK」渐变标题，8 秒后窗口
自动关闭，退出码 0；任一阶段失败会打印阶段标记并返回非 0。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk

from webview2_host import WebView2Host

TEST_HTML = """<!doctype html><html><head><meta charset="utf-8">
<style>
  body{margin:0;height:100vh;display:flex;flex-direction:column;justify-content:center;
       align-items:center;background:linear-gradient(160deg,#1E6FEB,#0DBD8B);
       color:#fff;font-family:'Segoe UI',sans-serif}
  h1{font-size:42px;margin:0} p{opacity:.85}
</style></head><body>
  <h1>WebView2 OK</h1><p>native ARM64 / x64 embed works</p>
</body></html>"""


def main():
    root = tk.Tk()
    root.title("WebView2 host test")
    root.geometry("900x600")
    frame = tk.Frame(root, background="#202124")
    frame.pack(fill="both", expand=True)
    status = tk.Label(root, text="creating WebView2 environment…", anchor="w")
    status.pack(fill="x")

    stages = {"env": False, "ctrl": False, "nav": False}

    data_dir = os.path.join(tempfile.mkdtemp(prefix="wv2-test-"), "udf")
    host = WebView2Host(frame.winfo_id(), data_dir,
                        on_error=lambda msg: (print("[on_error]", msg),
                                              status.config(
                                                  text="ERROR: %s" % msg,
                                                  fg="#c62828")))

    def on_created():
        print("[on_created] controller ready, webview ptr=%s"
              % bool(host._webview))
        stages["ctrl"] = bool(host._webview)     # 严格：拿到 ICoreWebView2 才算过
        if host._webview:
            host.navigate_to_string(TEST_HTML)
            stages["nav"] = True
        status.config(text="WebView2 ready & navigated (env+controller OK)")
        root.update_idletasks()

    def probe():
        print("[probe] source=%r visible=%s" % (host.get_source(),
                                                host.get_visible()))
        sys.stdout.flush()

    root.after(1500, probe)
    root.after(3000, probe)
    root.after(4500, probe)
    root.after(6000, probe)

    def shoot():
        # 裁决性诊断：ExecuteScript 探测页面真实内容（浏览器进程是否活着）。
        host.execute_script(
            'JSON.stringify({t: document.title, len: document.body.innerText.length})',
            lambda hr, res: print("[exec] hr=0x%08X result=%s" % (hr & 0xFFFFFFFF, res)))
        # 渲染自证：CapturePreview 直接出 PNG（锁屏/遮挡也不受影响）。
        out = os.path.join(tempfile.gettempdir(), "webview2-test-shot.png")
        try:
            ok = host.capture(out)
            print("capture ->", ok, out)
        except Exception as exc:
            print("capture failed: %r" % exc)
        sys.stdout.flush()

    root.after(5200, shoot)

    host._on_env_original = host._on_env

    def env_wrapper(this, hr, env):
        print("[env_invoke] hr=0x%08X env=%s" % (hr & 0xFFFFFFFF, bool(env)))
        result = host._on_env_original(this, hr, env)
        if env:
            stages["env"] = True
            status.config(text="environment created, creating controller…")
            root.update_idletasks()
        return result

    host._on_env = env_wrapper

    hr = host.start(on_created=on_created)
    print("start() -> hr=0x%08X" % (hr & 0xFFFFFFFF))
    sys.stdout.flush()

    def finish():
        print("stages:", stages)
        root.destroy()

    root.after(8000, finish)
    root.mainloop()
    ok = all(stages.values()) and hr == 0
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
