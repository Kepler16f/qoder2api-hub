# -*- coding: utf-8 -*-
"""prune_linux.py —— PyInstaller Linux 产物的 Qt 依赖闭包裁剪（spec 内调用）。

背景：PySide6 的 PyInstaller hook 会把整个 Qt 树（~140 个 libQt6*.so、qml、
全套翻译、全套 WebEngine locale）全量收进包，Linux 包因此膨胀到 ~230MB。
本模块按**实际 ELF 依赖图**决定去留（fail-open：任何解析异常都保留原状）：

  1. 整目录/整类黑名单先过一遍：Qt/qml（WebEngine 是 Chromium，不加载
     QML）、qtwebengine_devtools_resources.pak（壳无 DevTools 入口）、
     qtwebengine_locales 只留 en-US+zh-CN（看板中英文）、
     translations 只留 qtbase 中英文；
  2. 裁决集 = libQt6*.so + PySide6 的 *.abi3.so（Qt 绑定是桥，绑定本身
     也要按"壳是否 import"裁：Shell 只用 QtCore/QtGui/QtWidgets/
     QtWebEngineCore/QtWebEngineWidgets 五个）；
  3. 闭包根 = 保留的绑定 + Qt plugins 目录（平台/图片/输入法插件，运行期
     按名加载，静态 import 看不见）+ QtWebEngineProcess + 其余存活条目；
     沿 DT_NEEDED 传递闭包，裁决集中不在闭包内的删除（3D/图表/多媒体/
     定位/虚拟键盘等用不到的模块）。

实测该策略把 Linux 包从 ~230MB 降到 ~160MB；WebEngine 渲染进程所需的
QtWebEngineProcess / locales / icudtl.dat 全部保留。
"""
import os
import struct

# 壳真正 import 的 PySide6 绑定（desktop/qoder_desktop.py UI 3）
KEEP_ABI3 = {
    "QtCore.abi3.so",
    "QtGui.abi3.so",
    "QtWidgets.abi3.so",
    "QtWebEngineCore.abi3.so",
    "QtWebEngineWidgets.abi3.so",
}
# 必须保留的运行期资源（WebEngine 进程模型 / ICU / locale）
KEEP_BASENAME = {
    "QtWebEngineProcess",
    "icudtl.dat",
    "qtwebengine_resources.pak",
    "qtwebengine_resources_100p.pak",
    "qtwebengine_resources_200p.pak",
    "v8_context_snapshot.bin",
}
# WebEngine locale 白名单（看板中英文；其余 200+ 语言删除）
KEEP_LOCALES = {"en-US.pak", "zh-CN.pak"}
# Qt 翻译白名单：qtbase / qt 的中英文（标准对话框按钮本地化）
KEEP_TRANS = {
    "qtbase_zh_CN.qm", "qtbase_zh_TW.qm", "qtbase_en.qm", "qtbase_de.qm",
    "qt_zh_CN.qm", "qt_en.qm",
}


def _dt_needed(path):
    """读取 ELF64 的 DT_NEEDED 库名列表；非 ELF/解析失败返回 None。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
            if head[:4] != b"\x7fELF" or head[4] != 2:
                return None
            e_phoff = struct.unpack_from("<Q", head, 0x20)[0]
            e_phentsize = struct.unpack_from("<H", head, 0x36)[0]
            e_phnum = struct.unpack_from("<H", head, 0x38)[0]
            if not (0 < e_phnum <= 256) or e_phentsize < 56:
                return None
            fh.seek(e_phoff)
            ph = fh.read(e_phentsize * e_phnum)
            dyn = dynsz = None
            for i in range(e_phnum):
                off = i * e_phentsize
                if struct.unpack_from("<I", ph, off)[0] == 2:      # PT_DYNAMIC
                    dyn = struct.unpack_from("<Q", ph, off + 8)[0]
                    dynsz = struct.unpack_from("<Q", ph, off + 32)[0]
                    break
            if dyn is None or not (0 < dynsz <= (1 << 24)):
                return None
            fh.seek(dyn)
            ents = fh.read((dynsz // 16) * 16)
            pairs = []
            for o in range(0, len(ents), 16):
                tag, val = struct.unpack_from("<QQ", ents, o)
                if tag == 0:
                    break
                pairs.append((tag, val))
            strtab = next((v for t, v in pairs if t == 5), None)   # DT_STRTAB
            if strtab is None:
                return None
            fh.seek(strtab)
            blobs = fh.read(1 << 20)
            out = []
            for t, v in pairs:
                if t == 1:                                          # DT_NEEDED
                    end = blobs.find(b"\x00", v)
                    if end > 0:
                        out.append(blobs[v:end].decode("ascii", "replace"))
            return out
    except Exception:
        return None


def _is_qt(base):
    """PyInstaller 常把 Qt 库以扁平名收集（libQt6Core.so.6，不带 PySide6/
    前缀），所以裁决与根判定必须按 basename。"""
    return (base.startswith("libQt6") or base.startswith("libqt6")) \
        and (base.endswith(".so") or ".so." in base)


def _drop_static(name, say):
    """阶段 1：与依赖图无关的整类黑名单。返回 True=删。"""
    low = name.lower()
    base = name.rsplit("/", 1)[-1]
    if low.startswith("pyside6/qt/qml/") or "/qt/qml/" in low:
        say("[prune] drop %s (qml tree unused)" % name)
        return True
    if "qtwebengine_devtools_resources.pak" in low:
        say("[prune] drop %s (no devtools entry)" % name)
        return True
    if "qtwebengine_locales" in low:
        if base in KEEP_LOCALES:
            return False
        say("[prune] drop %s (locale not in %s)"
            % (name, sorted(KEEP_LOCALES)))
        return True
    if "/qt/translations/" in low:
        if base in KEEP_TRANS or base.startswith("pyside"):
            return False
        say("[prune] drop %s (translation not kept)" % name)
        return True
    return False


def prune_binaries(binaries, log=None):
    """过滤 PyInstaller 的 a.binaries（[(name, path, typecode), ...]）。

    任何内部异常都原样返回 binaries（fail-open，宁大勿坏）。
    """
    say = log or (lambda *a: None)
    try:
        return _prune(binaries, say)
    except Exception as exc:
        say("[prune] FAILED (%r) — keeping everything" % exc)
        return binaries


def _prune(binaries, say):
    survivors = []
    adjudged = []              # [(entry, base)] 待闭包裁决
    deps = {}                  # base -> [DT_NEEDED...]（None=解析失败）
    for entry in binaries:
        name, path = entry[0], entry[1]
        base = name.rsplit("/", 1)[-1]
        low = name.lower()
        if _drop_static(name, say):
            continue
        # 注意：PyInstaller 对 Qt 库用扁平名收集（libQt6*.so 不带 PySide6/
        # 前缀），所以裁决与根判定都必须按 basename 而非路径前缀。
        is_qt_lib = (base.startswith("libQt6") or base.startswith("libqt6")) \
            and (base.endswith(".so") or ".so." in base)
        is_binding = base.startswith("Qt") and base.endswith(".abi3.so")
        if is_binding and base not in KEEP_ABI3:
            # 壳不 import 的绑定：留着反而会把 QML/3D/Charts 子树拖回闭包
            say("[prune] drop %s (binding not imported)" % name)
            continue
        if is_qt_lib:
            adjudged.append((entry, base))
            if base not in deps:
                deps[base] = _dt_needed(path)
            continue
        survivors.append(entry)
        if base.endswith(".so") or ".so." in base or base in KEEP_BASENAME:
            if base not in deps:
                deps[base] = _dt_needed(path)

    # 闭包根：保留的 abi3 绑定 + Qt plugins（运行期 dlopen，静态分析看不见）
    # + WebEngine 进程 + libpython/gtk 等宿主系统库（不参与 Qt 闭包裁决）
    roots = set()
    for entry in survivors:
        name = entry[0]
        base = name.rsplit("/", 1)[-1]
        low = name.lower()
        if base in KEEP_BASENAME or base.endswith(".abi3.so") \
                or "/plugins/" in low or not _is_qt(base):
            roots.add(base)

    seen, stack = set(), list(roots)
    broken = False
    while stack:
        dep = stack.pop()
        if dep in seen:
            continue
        seen.add(dep)
        nd = deps.get(dep)
        if nd is None and dep in {b for _, b in adjudged}:
            # 裁决集内的库解析不出依赖：宁可全留
            broken = True
            break
        for nxt in (nd or ()):
            if nxt not in seen:
                stack.append(nxt)

    if broken:
        say("[prune] DT_NEEDED unparseable for some Qt libs — keeping all")
        survivors.extend(e for e, _ in adjudged)
        return survivors

    dropped = []
    for entry, base in adjudged:
        if base in seen:
            survivors.append(entry)
        else:
            dropped.append(entry[0])
    say("[prune] closure kept %d, dropped %d Qt/binding libs"
        % (len(adjudged) - len(dropped), len(dropped)))
    return survivors
