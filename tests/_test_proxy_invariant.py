"""代理/出站不变量套件：带凭证的请求必须走统一出站函数。

移植自 wb 的 tests/_test_account_proxy_calls.py 思路：不是「功能测试」，而是**不变量测试**
—— 只要有人新增一条绕过统一出站函数（http_json）的直连 urlopen，这里就要红。

    python tests/_test_proxy_invariant.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_accounts as A

PASS = FAIL = 0


def check(label, ok, detail=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %r" % (label, detail))


print("[proxy-invariant] 出站不变量")
calls = []
orig = A.http_json


def _stub(url, *a, **k):
    calls.append(url)
    raise RuntimeError("stub: 已拦在统一出站函数")


A.http_json = _stub
try:
    acc = A.Account({"uid": "pi1", "realm": "cn", "accessToken": "dt-x"})
    try:
        acc.fetch_credits()
    except Exception:
        pass
    n_fetch = len(calls)
finally:
    A.http_json = orig
check("fetch_credits 走统一出站函数 http_json（不直连）", n_fetch == 1, n_fetch)

# 基线登记：直连 urlopen 的调用点数量。改动这个数字之前，先确认新点是否带账号凭证。
# fork 校准：本仓库出站收口比上游更紧——所有带凭证调用（含上游收拢的
# http_json 内部与更新检查）都走 UPSTREAM_OPENER（逐请求代理感知，修
# WinError 10061 死端口 bug），故 qoder_accounts 直连为 0；qoder_proxy 仅剩
# 本地端口探测 1 处（127.0.0.1，无代理语义）。新增直连前先评估：带凭证或
# 公网访问的调用一律走 UPSTREAM_OPENER。
src_a = open(os.path.join(ROOT, "qoder_accounts.py"), encoding="utf-8").read()
n_direct_a = src_a.count("urllib.request.urlopen(")
src_p = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
n_direct_p = src_p.count("urllib.request.urlopen(")
check("qoder_accounts.py 直连 urlopen 基线 == 0（fork：凭证出站全走 UPSTREAM_OPENER）",
      n_direct_a == 0, n_direct_a)
check("qoder_proxy.py 直连 urlopen 基线 == 1（fork：仅本地端口探测）",
      n_direct_p == 1, n_direct_p)

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed" % (PASS + FAIL, PASS, FAIL))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
