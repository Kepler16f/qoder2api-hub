# tests/ —— 套件化测试（P0-2 骨架）

## 怎么跑

    python tests/run_all.py              # 全部套件（并发，一行一套件）
    python tests/run_all.py guards       # 只跑名字含 guards 的
    python _test_qoder.py                # 老入口仍然有效（保留兼容）

编排照抄 wb 的 `tests/run_all.py`：`ThreadPoolExecutor` 并发、每套件写独立日志文件、
失败打印 tail 25 行、子进程强制 `PYTHONIOENCODING=utf-8`、无匹配套件返回 2。

## 现有套件

| 套件 | 覆盖 | 状态 |
|---|---|---|
| `_test_legacy.py` | 把仓根 `_test_qoder.py` 整体接进新编排（**689 条既有断言一条不丢**） | 迁移过渡用 |
| `_test_guards.py` | P0-1 护栏核心不变量（含等号边界 / fail-open / free 豁免 / 全关短路） | 新 |
| `_test_auth_matrix.py` | 面板鉴权矩阵（401 / 200-面板会话 / 403 语义与 401 分离） | 新 |

## 迁移计划（两步交付的第一步已落地）

1. **已完成**：编排骨架 + 兼容入口 + 两个新套件；
2. **待迁**：把 `_test_qoder.py` 的 [1]-[44] 段按主题拆成 `_test_*.py`，每拆一段就从 legacy 里删掉一段，
   直到 `_test_legacy.py` 只剩空壳；
3. **待补的三类空白**：代理不变量（参数穿透式断言）、keep-alive 早拒（裸 socket 复现）、
   前端 DOM 断言（照 wb 的 `_dom_stub.js`）。

## 观测点原则（来自 C3 那五轮的教训）

**判据要拦在对的那一层**：说「没有发上游」就要拦在**网络层**（`http_json`），
而不是「`open_upstream` 是否被调用」—— 后者在正常路径上本来就会被调用（它内部才选号）。

## 本轮落盘实况（task-71 第一步）

**迁移进度：已迁 0 段 / 待迁 44 段。** 既有 689 条断言全部由 `_test_legacy.py` 承载（转发 `_test_qoder.py`），
不丢一条；新增 2 个独立套件（guards 10 条 + auth-matrix 5 条）。

### run_all.py 输出样例

    $ python tests/run_all.py
      _test_auth_matrix.py               PASS
      _test_guards.py                    PASS
      _test_legacy.py                    PASS

    SUMMARY: 3 suites, 3 passed, 0 failed  (logs: <tmp>/qd-suites-xxxx)
    $ echo $?
    0

失败时会额外打印该套件的 tail 25 行；无套件匹配时返回 2（避免「0 passed, 0 failed → exit 0」这种 CI 最怕的假绿）。

### 故意改坏 → 必红（自证记录）

变异 `qoder_accounts.py` 里 `reserve_blocked` 的判定式 `int(float(remain)) <= int(reserve)` → 改成 `<`：

    $ python tests/_test_guards.py
      [FAIL] reserve 含等号：remain==reserve -> 拦  None
    SUMMARY: TOTAL 10 checks, 9 passed, 1 failed
    RESULT: RED (exit 1)

恢复后同一套件 `GREEN (exit 0)`；且 `git diff --stat -- qoder_accounts.py` 为空 —— 变异只在备份副本上做、原文件未留痕。

### 三条提醒的落点

1. **判据拦在对的层**：见 README 上节「观测点原则」，guards 套件直接测判定函数本身；
2. **兼容入口保留**：`python _test_qoder.py` 原样可用，`tests/run_all.py` 通过 `_test_legacy.py` 转发它，两边同源；
3. **两步交付**：这一步只做「能跑、能汇总、能红」的骨架 + 2 个新套件，不追求一次拆完 4873 行。

## 第二步：三类空白套件已补（task-71 · 第一批）

| 套件 | 类型 | 覆盖 |
|---|---|---|
| `_test_proxy_invariant.py` | 不变量 | 带凭证的请求必须走统一出站函数 `http_json`（桩它 → 断言 `fetch_credits` 经过它）；**基线登记**两个文件里直连 `urlopen` 的调用点数量（新增/删除都要重新评估是否绕过收口） |
| `_test_keepalive.py` | 裸 socket | ① 超长请求行（100KB URI）必须**快速拒绝**（实测 414，服务端随后直接关连接，客户端 recv 可能抛 `ConnectionAbortedError` —— 两者都算「没挂起」）；② **同一条连接**连发两个 `/health` 都要 200（读响应必须按 `Content-Length` 读满 body，否则第二次会读到上一次的 body 残片） |
| `_test_dashboard_render.js` | 前端（层 1+2） | 读 `dashboard.html` → 抽所有 `<script>` → **内联约 15 行元素桩**（照 wb `_test_matrix_filters.js` 的写法；工作包里提到的 `_dom_stub.js` 在 wb 里并不存在）→ 断言渲染纯函数四态与 toast 配色，另加一条「日志区是追加式写法」的源码级断言 |

> `tests/` 里现在有 **1 个 .js 套件**（此前 0 个）；`run_all.py` 会自动发现并跳过 node 缺失的环境。

### 能红自证（三条）

1. **guards**：`reserve_blocked` 的 `<=` 改成 `<` → `[FAIL] reserve 含等号` → RED exit 1；恢复后 GREEN；`git diff -- qoder_accounts.py` 无我方痕迹。
2. **proxy_invariant**：往 `qoder_accounts.py` 追加一处直连 `urlopen`（基线断言 == 3）→ RED exit 1；恢复后 GREEN。
3. **dashboard_render**：把 `dashboard.html` 里 `\|\| earned > 0` 改成 `>= 0` →
   `[FAIL] checkinOutcome：earned=0 且无 claimed -> idle（不报签到成功）` + `[FAIL] checkinToastKind：全 idle -> warn` → RED exit 1；恢复后 GREEN exit 0。

   这三条都只动**备份副本**（`%TEMP%` 下先备份、跑完立即还原），原文件不留痕。
