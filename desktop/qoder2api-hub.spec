# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec —— 六平台共享一份（平台差异在 spec 内按 sys.platform 分支）。

产物形态：
    Windows / Linux : onefile 单文件可执行（dist/Qoder2API-Hub[.exe]，免安装）
    macOS           : onedir + .app 包（dist/Qoder2API-Hub.app，随后 zip 分发）

UMID 组件：CI 在 PyInstaller 之前运行 `python _install_umid.py --dest umid`
（提取失败不阻断构建），本 spec 检测到 umid/runtime-info 时自动打入
_meipass/umid/，桌面壳运行时经 QD_UMID_DIR 暴露给网关。

    python -m PyInstaller desktop/qoder2api-hub.spec --noconfirm \
        --distpath dist --workpath build
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(SPEC))          # desktop/
ROOT = os.path.dirname(HERE)                            # 仓库根
NAME = "Qoder2API-Hub"

# 网关运行期以 __file__ 定位读取的只读资源 → 平铺进 _MEIPASS
datas = [
    (os.path.join(ROOT, "dashboard.html"), "."),
    (os.path.join(ROOT, "baseprompt.json"), "."),
    (os.path.join(ROOT, "qoder_catalog_intl.json"), "."),
    (os.path.join(ROOT, "qoder_catalog_cn.json"), "."),
    (os.path.join(HERE, "assets", "qoder2api.png"), "."),
]

# Windows：WebView2 加载器（arm64 + x64 都带上，运行时按进程架构选）。
# 加载器取自 NuGet Microsoft.Web.WebView2（重分发许可允许随应用分发）。
if sys.platform == "win32":
    wv2_dir = os.path.join(HERE, "assets", "webview2")
    for dll in ("WebView2Loader-arm64.dll", "WebView2Loader-x64.dll"):
        p = os.path.join(wv2_dir, dll)
        if os.path.isfile(p):
            datas.append((p, os.path.join("assets", "webview2")))

# UMID 原生组件（存在才打；放 binaries 保留执行位，POSIX 运行必需）
binaries = []
umid_name = "runtime-info.exe" if sys.platform == "win32" else "runtime-info"
umid_path = os.path.join(ROOT, "umid", umid_name)
if os.path.isfile(umid_path):
    binaries.append((umid_path, "umid"))

if sys.platform == "win32":
    icon = os.path.join(HERE, "assets", "qoder2api.ico")
elif sys.platform == "darwin":
    icon = os.path.join(HERE, "assets", "qoder2api.icns")
else:
    icon = None

onefile = sys.platform != "darwin"

# pywebview 按平台在运行期动态 import 后端，PyInstaller 静态分析看不到，
# 需要显式声明（Linux 走 PySide6 不用 pywebview；Windows 走自研 WebView2 宿主）。
hiddenimports = []
if sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]

a = Analysis(
    [os.path.join(HERE, "qoder_desktop.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

if onefile:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=NAME,
        console=False,          # 无控制台窗口；输出经桌面壳 Tee 进日志面板/文件
        icon=icon,
        upx=False,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=NAME,
        console=False,
        icon=icon,
        upx=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        name=NAME,
    )

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=NAME + ".app",
        icon=icon,
        bundle_identifier="io.github.shuishuipingan.qoder2api-hub",
        info_plist={"CFBundleDisplayName": NAME,
                    "NSHighResolutionCapable": True},
    )
