# Qoder2API-Hub 桌面客户端

双击即用的桌面版：**窗口内直接内嵌 Web 看板**（与网页版功能 100% 对等——账号
管理、OAuth 添加、每日签到与福利领取、模型清单、用量指标、实时日志，全部在
应用窗口里完成），不依赖外部浏览器。关闭窗口即优雅停机，不留后台进程，
延续主项目「生命周期就是那个窗口」的设计。

## 各平台内嵌实现（全部自适应，内嵌不可用自动回退控制台 + 浏览器）

| 平台 | 内嵌方案 | 说明 |
|---|---|---|
| Windows x64 | tkinter + **WebView2 原生嵌入**（`webview2_host.py`，纯标准库 ctypes 手写 COM） | Win10/11 自带 Evergreen WebView2 运行时 |
| Windows ARM64 | 同上，**原生运行**（不用仿真） | clr_loader/pythonnet 不支持 arm64，QtWebEngine 无 win-arm64 构建，故自研 ctypes 宿主；WebView2 加载器按进程架构选 arm64/x64 |
| macOS Apple Silicon | **pywebview**（WKWebView，系统自带） | pip 纯轮子（pyobjc），无系统依赖 |
| Linux x64 | **PySide6 + QtWebEngine** | 要求 glibc ≥ 2.28（Ubuntu 20.04+ 等） |
| Linux ARM64 | 同上 | 要求 glibc ≥ 2.39（Ubuntu 24.04+ 等新发行版） |
| 任意平台兜底 | tkinter 控制台 + 浏览器打开看板 | `--ui console` 可强制；日志面板/端口/LAN/Key 复制齐全 |

## 下载与使用

到 GitHub Releases 下载对应平台产物（打 `v*` tag 时由
`.github/workflows/desktop-build.yml` 自动构建发布）：

| 文件 | 适用 |
|---|---|
| `Qoder2API-Hub-windows-x64.exe` | Windows 10/11 x64 |
| `Qoder2API-Hub-windows-arm64.exe` | Windows on ARM（Surface Pro X / Snapdragon 笔记本等，原生） |
| `Qoder2API-Hub-macos-arm64.zip` | Apple Silicon Mac（M1/M2/M3/M4） |
| `Qoder2API-Hub-linux-x64` | 主流发行版 x64（glibc ≥ 2.28） |
| `Qoder2API-Hub-linux-arm64` | ARM64 Linux（glibc ≥ 2.39，如 Ubuntu 24.04+） |

- **Windows**：双击 exe 即可。首次运行 SmartScreen 可能提示「已保护你的电脑」
  ——点「更多信息 → 仍要运行」（未购买代码签名证书的通病）。
- **macOS**：解压 zip 得到 `Qoder2API-Hub.app`，拖进「应用程序」；首次右键
  →「打开」（ad-hoc 签名无开发者账号，Gatekeeper 会拦直接双击）。若仍被拦：
  `xattr -cr /Applications/Qoder2API-Hub.app`。
- **Linux**：`chmod +x Qoder2API-Hub-linux-x64 && ./Qoder2API-Hub-linux-x64`。
  需要系统有基础 GUI 库（桌面发行版默认齐全）。
- 首次进入看板的面板密码是 `admin`，请立即在「设置」里修改（与 API Key 相互独立）。

## 数据目录（账号凭证 / 用量 / 日志，均为明文 token，注意保管）

| 平台 | 位置 | 覆盖方式 |
|---|---|---|
| Windows / Linux | exe 同目录（便携模式；目录只读时自动回退 `%APPDATA%\qoder2api-hub` / `~/.local/share/qoder2api-hub`） | 环境变量 `QD_DATA_DIR` |
| macOS | `~/Library/Application Support/qoder2api-hub/` | 同上 |

- `accounts/` 账号凭证、`usage/` 请求流水、`desktop-gateway.log` 运行日志、
  `desktop.json` 窗口记忆（端口 / 局域网开关）、`webview2-udf/` 浏览器配置。
- ⚠️ `machine_identity.json`（若有）代表本机设备身份，**不要跨机器共享**。

## 从源码运行（不打包直接用）

```bash
# Windows（需要本机 Python 3.9+，装了官方 Qoder 客户端还能白拿真身识别）
python desktop/qoder_desktop.py

# macOS（内嵌 WKWebView 需要一次性安装）
python -m pip install pywebview
python desktop/qoder_desktop.py

# Linux x64 / ARM64 新发行版（内嵌 QtWebEngine，可选）
python -m pip install PySide6
python desktop/qoder_desktop.py

# 不想装任何 GUI 依赖：控制台模式（功能等价，看板走浏览器）
python desktop/qoder_desktop.py --ui console
```

## 从源码构建各平台产物

PyInstaller 不支持交叉编译，需在目标平台上执行（CI 的 5 条产线即此流程）：

```bash
python -m pip install pyinstaller
# macOS 追加:  python -m pip install pywebview
# Linux 追加:  python -m pip install PySide6
# POSIX（可选，领每日 Credits 的真身识别）:
python _install_umid.py --platform linux --arch x86_64 --dest umid   # 参数见脚本

python -m PyInstaller desktop/qoder2api-hub.spec --noconfirm
# Windows/Linux → dist/Qoder2API-Hub(.exe) 单文件
# macOS        → dist/Qoder2API-Hub.app
```

Intel 版 macOS：GitHub 已退役 macos-13 产线，需要的话在 Intel Mac 上按上面
源码构建步骤执行即可（`pywebview` 在 Intel 轮子齐全）。

## 自检命令（CI 同款）

```bash
Qoder2API-Hub.exe --smoke-test        # 无 GUI：起网关→/health→/v1/models→/→优雅停机
python desktop/qoder_desktop.py --smoke-test     # 源码同款
python desktop/qoder_desktop.py --smoke-gui      # 开窗→自动关闭（验证 GUI 链路）
```

## 诊断

- 看板打不开 / 白屏：先看 `desktop-gateway.log`；内嵌失败会自动回退控制台
  并在状态栏注明原因（例如「WebView2 不可用」→ 装 [WebView2 运行时](
  https://developer.microsoft.com/microsoft-edge/webview2/) 或换控制台模式）。
- 网关链路体检沿用主项目的工具：`python _diag_gateway.py --chat`、
  `python _diag_campaign.py`。

## 已知限制

- 每日 Credits 等活动的**真身识别**：Windows 上需本机装有官方 Qoder 桌面客户端
  （网关自动发现其 `runtime-info.exe`）；没有则退化为派生身份，聊天/API 不受
  影响，但活动可能被服务端过滤（详见主 README issue #10/#18 说明）。
- Linux ARM64 内嵌要求较新发行版（glibc ≥ 2.39）；旧发行版自动回退控制台模式。
- 未做代码签名：Windows SmartScreen / macOS Gatekeeper 首次运行需手动放行。
