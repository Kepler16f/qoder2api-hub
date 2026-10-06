#!/usr/bin/env python3
"""_test_nav_guard.py —— 返回手势屏蔽真机测试（开窗自测，约 12 秒自动关闭）。

    python desktop/_test_nav_guard.py

验证 webview2_host.harden_navigation()（纯 Settings 写，无事件回调）：
  1. harden 在 UI 事件循环里调用成功（get_Settings + 两个 put_* 都 S_OK）；
  2. 调用后页面仍能正常导航、脚本执行正常（防护不影响壳自身流程）；
  3. 干净关窗（真崩溃场景 = 拆卸阶段投递事件——本方案不注册回调，
     由 --smoke-gui 稳定性回归兜底）。

退出码 0 = 全部通过。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tkinter as tk

from webview2_host import WebView2Host


def page(title, tone):
    return ("<!doctype html><html><head><meta charset='utf-8'><title>%s</title>"
            "<style>body{margin:0;height:100vh;display:flex;align-items:center;"
            "justify-content:center;background:%s;color:#fff;"
            "font-family:'Segoe UI',sans-serif;font-size:40px}</style></head>"
            "<body>%s</body></html>" % (title, tone, title))


def main():
    root = tk.Tk()
    root.title("nav harden test")
    root.geometry("720x460")
    frame = tk.Frame(root, background="#111")
    frame.pack(fill="both", expand=True)
    data_dir = os.path.join(tempfile.mkdtemp(prefix="wv2-nav-"), "udf")
    host = WebView2Host(frame.winfo_id(), data_dir,
                        on_error=lambda msg: print("[on_error]", msg))
    results = {}

    def title_now(cb):
        host.execute_script(
            "document.title",
            lambda hr, res: cb(str(res).strip().strip('"')))

    def expect(name, got, want):
        ok = got == want
        results[name] = ok
        print("[check] %-24s got=%-10r want=%-10r %s"
              % (name, got, want, "OK" if ok else "FAIL"))
        sys.stdout.flush()

    def step_live():
        host.navigate_to_string(page("LIVE", "#1E6FEB"))
        root.after(1200, lambda: title_now(
            lambda t: expect("page loads after harden", t, "LIVE") or finish()))

    def finish():
        ok = bool(results) and all(results.values())
        print("RESULT:", "OK" if ok else "FAIL", results)
        sys.stdout.flush()
        root.destroy()

    def on_created():
        def arm():
            ok = host.harden_navigation()
            print("[arm] harden_navigation ->", ok)
            sys.stdout.flush()
            results["harden succeeds"] = bool(ok)
            step_live()
        # ⚠️ 必须回事件循环再调（COM 回调栈上跨进程调用会泵消息崩线程状态）；
        # 延时非零——after(0) 会在当前泵迭代里立即重入。
        root.after(50, arm)

    host.start(on_created=on_created)
    root.after(15000, finish)          # 兜底
    root.mainloop()
    return 0 if results and all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
