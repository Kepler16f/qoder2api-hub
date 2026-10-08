"""护栏套件（P0-1 事实性限额护栏的核心不变量）。

    python tests/_test_guards.py

判据一律拦在**判定函数本身**（reserve_blocked / daily_limit_blocked / ... ），
不依赖网络、不依赖选号路径 —— 这是 C3 那五轮学到的教训：观测点要选在能被
「守卫是否生效」直接决定的那一层。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
import qoder_accounts as A

PASS = FAIL = SKIP = 0


def check(label, cond, extra=None):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %r" % (label, extra))


def acc(**kw):
    a = A.Account({"uid": kw.pop("uid", "g1"), "realm": "cn",
                   "accessToken": "dt-x"})
    a.enabled = True
    a.expires_at = time.time() + 3600
    for k, v in kw.items():
        setattr(a, k, v)
    return a


print("[analytics-parity] P1-3 by_key ≡ by_account 同源不变量（夹具来自 natie）")

import qoder_proxy as P


def check_by_key_semantics(check):
    """P1-3 by_key 聚合：三类边界 + 同源不变量（夹具来自 natie）。

    边界：① key_id 缺失行 -> "(no-key)" 桶且不丢弃；② error 行 -> 不计入 token/credit；
         ③ 跨日行 -> 窗口按 row["at"] 过滤（闭区间），与 by_account 逐行同判。

    注意（natie 的口径）：keys 桶数可以少于 accounts 桶数（一个 Key 可能打多个账号）——
    桶数不等不是 bug，判据是**同窗口下的合计相等**；且必须用固定夹具（真实日志仍在
    追加 error 行，合计类断言用真实日志会随增长漂移、被误判成回归）。
    """
    import json as _json2
    import os as _os2
    import shutil as _sh2
    import tempfile as _tf2
    import time as _t2

    today = _t2.time()
    yday = today - 86400

    def iso(t):
        return _t2.strftime("%Y-%m-%dT%H:%M:%S", _t2.localtime(t))

    rows = [
        {"at": today, "iso": iso(today), "model": "M1", "account": "u1", "key_id": "k00001",
         "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "credit": 1.0},
        {"at": today, "iso": iso(today), "model": "M2", "account": "u2", "key_id": "k00002",
         "prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25, "credit": 0.5},
        {"at": today, "iso": iso(today), "model": "M1", "account": "u1",
         "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "credit": 0.1},
        {"at": today, "iso": iso(today), "model": "M1", "account": "u1", "key_id": "k00001",
         "error": "boom", "prompt_tokens": 999, "total_tokens": 999, "credit": 9.9},
        {"at": yday, "iso": iso(yday), "model": "M-old", "account": "u1", "key_id": "k00001",
         "prompt_tokens": 500, "total_tokens": 500, "credit": 5.0},
    ]
    d = _tf2.mkdtemp(prefix="qd-bykey-")
    log = _os2.path.join(d, "usage.jsonl")
    with open(log, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(_json2.dumps(r, ensure_ascii=False) + "\n")
        fh.write("\n")             # 空行
        fh.write("{not json}\n")   # 坏 JSON 行
    orig = P.USAGE_LOG
    P.USAGE_LOG = log
    try:
        allk = {b["key_id"]: b for b in P._usage_by_key_uncached()}
        alla = {b["account"]: b for b in P._usage_by_account_uncached()}
        _t = _t2.localtime()
        lo = _t2.mktime((_t.tm_year, _t.tm_mon, _t.tm_mday, 0, 0, 0, 0, 0, -1))
        dayk = {b["key_id"]: b for b in P._usage_by_key_uncached(since=lo)}
        daya = {b["account"]: b for b in P._usage_by_account_uncached(since=lo)}
    finally:
        P.USAGE_LOG = orig
        _sh2.rmtree(d, ignore_errors=True)

    check("by_key: 缺 key_id 的行归 (no-key) 桶且不丢弃",
          set(allk) == {"k00001", "k00002", "(no-key)"}, sorted(allk))
    check("by_key: error 行不计入（无窗口 k00001 = 今天 110 + 昨日 500 = 2 条/610）",
          allk["k00001"]["requests"] == 2
          and allk["k00001"]["total_tokens"] == 610
          and abs(allk["k00001"]["credit"] - 6.0) < 1e-9,
          (allk["k00001"]["requests"], allk["k00001"]["total_tokens"]))
    check("by_key: 跨日行被 day 窗口排除（无窗口 645 -> day 145）",
          sum(b["total_tokens"] for b in allk.values()) == 645
          and sum(b["total_tokens"] for b in dayk.values()) == 145
          and dayk["k00001"]["requests"] == 1,
          (sum(b["total_tokens"] for b in allk.values()),
           sum(b["total_tokens"] for b in dayk.values()),
           dayk.get("k00001", {}).get("requests")))
    for tag, k, a in (("全量", allk, alla), ("day", dayk, daya)):
        check("by_key ≡ by_account（%s 窗口合计相等）" % tag,
              sum(b["total_tokens"] for b in k.values())
              == sum(b["total_tokens"] for b in a.values())
              and sum(b["requests"] for b in k.values())
              == sum(b["requests"] for b in a.values()),
              (tag, sum(b["total_tokens"] for b in k.values()),
               sum(b["total_tokens"] for b in a.values())))

def check_natie_s15(check):
    """§15 修订版夹具（natie 提供，期望值写死）：7 类行 × 桶数/窗口边界。

    覆盖：正常行 / 一 Key 打多账号 / error 行 / **缺 at** / 缺 key_id / 缺 account / 跨日行。
    时间锚用本地零点 ±30min —— 不用 time.time()，否则测试恰跑在零点附近会假失败。
    """
    import json as _j
    import os as _o
    import shutil as _s
    import tempfile as _t
    import time as _tm

    _lt = _tm.localtime()
    mid = _tm.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday, 0, 0, 0, 0, 0, -1))
    t_today = mid + 1800          # 今天 00:30 —— 必在 day 窗口内
    t_yday = mid - 1800           # 昨天 23:30 —— 必在 day 窗口外

    def iso(t):
        return _tm.strftime("%Y-%m-%dT%H:%M:%S", _tm.localtime(t))

    rows = [
        {"at": t_today, "iso": iso(t_today), "model": "M1", "account": "u1", "key_id": "k1",
         "prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10, "credit": 1.0},
        {"at": t_today, "iso": iso(t_today), "model": "M1", "account": "u2", "key_id": "k1",
         "prompt_tokens": 6, "completion_tokens": 5, "total_tokens": 11, "credit": 1.1},
        {"at": t_today, "iso": iso(t_today), "model": "M1", "account": "u1", "key_id": "k1",
         "error": "boom", "prompt_tokens": 999, "total_tokens": 999, "credit": 9.9},
        {"iso": iso(t_today), "model": "M1", "account": "u1", "key_id": "k1",
         "prompt_tokens": 20, "total_tokens": 20, "credit": 2.0},
        {"at": t_today, "iso": iso(t_today), "model": "M1", "account": "u1",
         "prompt_tokens": 30, "total_tokens": 30, "credit": 3.0},
        {"at": t_today, "iso": iso(t_today), "model": "M1", "key_id": "k1",
         "prompt_tokens": 40, "total_tokens": 40, "credit": 4.0},
        {"at": t_yday, "iso": iso(t_yday), "model": "M1", "account": "u1", "key_id": "k1",
         "prompt_tokens": 100, "total_tokens": 100, "credit": 10.0},
    ]
    d = _t.mkdtemp(prefix="qd-s15-")
    log = _o.path.join(d, "usage.jsonl")
    with open(log, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(_j.dumps(r, ensure_ascii=False) + "\n")
        fh.write("\n")            # 空行：所有消费者都跳过
        fh.write("{not json}\n")  # 坏行：只有 count_usage_rows 会计
    orig = P.USAGE_LOG
    P.USAGE_LOG = log
    try:
        allk = {b["key_id"]: b for b in P._usage_by_key_uncached()}
        alla = {b["account"]: b for b in P._usage_by_account_uncached()}
        dayk = {b["key_id"]: b for b in P._usage_by_key_uncached(since=mid)}
        daya = {b["account"]: b for b in P._usage_by_account_uncached(since=mid)}
    finally:
        P.USAGE_LOG = orig
        _s.rmtree(d, ignore_errors=True)

    check("§15 桶数可不等而合计相等（一 Key 打多账号：keys < accounts）",
          len(allk) < len(alla)
          and sum(b["total_tokens"] for b in allk.values())
          == sum(b["total_tokens"] for b in alla.values()),
          (len(allk), len(alla),
           sum(b["total_tokens"] for b in allk.values())))
    check("§15 缺 at 行：day 窗口按 at=0 排除，且两侧同判（k1=61 / u1=40）",
          dayk["k1"]["total_tokens"] == 61 and daya["u1"]["total_tokens"] == 40,
          (dayk["k1"]["total_tokens"], daya["u1"]["total_tokens"]))
    check("§15 day 窗口合计 91 == 91；无窗口 211 == 211（error 行两侧都跳过）",
          sum(b["total_tokens"] for b in dayk.values()) == 91
          and sum(b["total_tokens"] for b in daya.values()) == 91
          and sum(b["total_tokens"] for b in allk.values()) == 211
          and sum(b["total_tokens"] for b in alla.values()) == 211,
          (sum(b["total_tokens"] for b in dayk.values()),
           sum(b["total_tokens"] for b in allk.values())))
    check("§15 无窗口 by_key k1 = (5, 181)（含缺 at 的 20 与昨日 100）",
          allk["k1"]["requests"] == 5 and allk["k1"]["total_tokens"] == 181,
          (allk["k1"]["requests"], allk["k1"]["total_tokens"]))


check_natie_s15(check):
    """P1-3 by_key 聚合：三类边界 + 同源不变量（natie 提供 / task-75）。

    边界：① key_id 缺失行 -> "(no-key)" 桶且不丢弃；② error 行 -> 不计入 token/credit；
         ③ 跨日行 -> 窗口过滤与 by_account 逐行同判。

    注意（natie 的口径）：keys 桶数可以少于 accounts 桶数（一个 Key 可能打多个账号）——
    桶数不等不是 bug，判据是**同窗口下的合计相等**。真实数据当前只有 (no-key) 一个桶，
    所以多 Key 形态必须用这份夹具造；且必须用夹具（真实日志仍在追加 error 行，
    合计类断言用真实日志会随增长漂移）。
    """
    import json as _json2
    import os as _os2
    import shutil as _sh2
    import tempfile as _tf2
    import time as _t2

    today = _t2.time()
    yday = today - 86400

    def iso(t):
        return _t2.strftime("%Y-%m-%dT%H:%M:%S", _t2.localtime(t))

    rows = [
        {"at": today, "iso": iso(today), "model": "M1", "account": "u1", "key_id": "k00001",
         "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110, "credit": 1.0},
        {"at": today, "iso": iso(today), "model": "M2", "account": "u2", "key_id": "k00002",
         "prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25, "credit": 0.5},
        # 边界①：key_id 缺失（P1-3 字段上线前的历史行形态）
        {"at": today, "iso": iso(today), "model": "M1", "account": "u1",
         "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10, "credit": 0.1},
        # 边界②：error 行（不得计入任何 token/credit）
        {"at": today, "iso": iso(today), "model": "M1", "account": "u1", "key_id": "k00001",
         "error": "boom", "prompt_tokens": 999, "total_tokens": 999, "credit": 9.9},
        # 边界③：跨日行（day 窗口必须排除）
        {"at": yday, "iso": iso(yday), "model": "M-old", "account": "u1", "key_id": "k00001",
         "prompt_tokens": 500, "total_tokens": 500, "credit": 5.0},
    ]
    d = _tf2.mkdtemp(prefix="qd-bykey-")
    log = _os2.path.join(d, "usage.jsonl")
    with open(log, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(_json2.dumps(r, ensure_ascii=False) + "\n")
        fh.write("\n")             # 空行
        fh.write("{not json}\n")   # 坏 JSON 行
    orig = P.USAGE_LOG
    P.USAGE_LOG = log
    try:
        allk = {b["key_id"]: b for b in P._usage_by_key_uncached()}
        alla = {b["account"]: b for b in P._usage_by_account_uncached()}
        # 按 natie 的口径：窗口比较的是 row["at"]（epoch），完全不看 iso；
        # 这里自算本地零点（不依赖 _local_midnight 是否暴露），造夹具必须给 at ——
        # 只给 iso 会被当成 at=0，跨日断言会假失败。
        _t = _t2.localtime()
        lo = _t2.mktime((_t.tm_year, _t.tm_mon, _t.tm_mday, 0, 0, 0, 0, 0, -1))
        dayk = {b["key_id"]: b for b in P._usage_by_key_uncached(since=lo)}
        daya = {b["account"]: b for b in P._usage_by_account_uncached(since=lo)}
    finally:
        P.USAGE_LOG = orig
        _sh2.rmtree(d, ignore_errors=True)

    check("by_key: 缺 key_id 的行归 (no-key) 桶且不丢弃",
          set(allk) == {"k00001", "k00002", "(no-key)"}, sorted(allk))
    check("by_key: error 行不计入（无窗口 k00001 = 今天 110 + 昨日 500 = 2 条/610）",
          allk["k00001"]["requests"] == 2
          and allk["k00001"]["total_tokens"] == 610
          and abs(allk["k00001"]["credit"] - 6.0) < 1e-9,
          (allk["k00001"]["requests"], allk["k00001"]["total_tokens"]))
    # 口径（natie §15）：窗口比较 row["at"]（epoch），闭区间；无窗口 = 全量。
    # 本夹具：今天 3 条成功行 = 145 tokens，昨日 1 条 = 500 → 无窗口 645、day 窗口 145。
    check("by_key: 跨日行被 day 窗口排除（无窗口 645 → day 145）",
          sum(b["total_tokens"] for b in allk.values()) == 645
          and sum(b["total_tokens"] for b in dayk.values()) == 145
          and dayk["k00001"]["requests"] == 1,
          (sum(b["total_tokens"] for b in allk.values()),
           sum(b["total_tokens"] for b in dayk.values()),
           dayk.get("k00001", {}).get("requests")))

    for tag, k, a in (("全量", allk, alla), ("day", dayk, daya)):
        check("by_key ≡ by_account（%s 窗口合计相等）" % tag,
              sum(b["total_tokens"] for b in k.values())
              == sum(b["total_tokens"] for b in a.values())
              and sum(b["requests"] for b in k.values())
              == sum(b["requests"] for b in a.values()),
              (tag, sum(b["total_tokens"] for b in k.values()),
               sum(b["total_tokens"] for b in a.values())))


check_by_key_semantics(check)

print("")
print("SUMMARY: TOTAL %d checks, %d passed, %d failed, %d skipped" % (PASS + FAIL + SKIP, PASS, FAIL, SKIP))
print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN", 1 if FAIL else 0))
sys.exit(1 if FAIL else 0)
