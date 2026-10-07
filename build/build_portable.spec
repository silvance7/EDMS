# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— 绿色便携版（onedir）。

    图标是 ico/EDMS.ico（静态文件，仓库里有一份），换图标 = 覆盖它再重打。
    .venv/Scripts/python.exe -m PyInstaller build/build_portable.spec --noconfirm
    （完整命令含 --workpath / --distpath，见 README「命令速查」）

**为什么用 onedir 而不是 onefile**：
    onefile 每次启动都要先把整个包解压到临时目录，冷启动 3–5 秒；
    onedir 直接运行，1 秒内。你要的是"双击就能用"，不是"只有一个文件"。
    代价是产物是个文件夹——但这本来就更适合自用工具：
    整个文件夹拷到 U 盘、插到另一台电脑上照样能用，数据跟着走。

**数据放哪**：由 app/paths.py 决定，固定是 exe 旁边的 data/。
    绝不能放进 _MEIPASS（打包资源目录）——onefile 下那是临时目录，退出即删；
    哪怕是 onedir，放 _internal 里也会在下次重新打包时被覆盖。
"""

from pathlib import Path

PROJECT = Path(SPECPATH).parent          # SPECPATH 是 build/，上一级才是项目根

# 明确排除用不到的 Qt 模块，产物能小一大截。
# 本应用只用到 QtCore / QtGui / QtWidgets，其余全是打包垃圾。
EXCLUDES = [
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel", "PySide6.QtWebSockets",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtHelp", "PySide6.QtDesigner", "PySide6.QtUiTools",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtLocation", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtSensors", "PySide6.QtTextToSpeech", "PySide6.QtRemoteObjects",
    "PySide6.QtScxml", "PySide6.QtStateMachine", "PySide6.QtConcurrent",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.QtNetwork",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtXml", "PySide6.QtDBus",
    # 标准库里用不到的大块头
    "tkinter", "unittest", "pydoc_data", "lib2to3", "test",
    "sqlite3.test", "distutils", "setuptools", "pip", "test.support",
]

a = Analysis(
    [str(PROJECT / "run.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=[
        # schema.sql 是运行期要读的只读资源，必须打进去。
        # 目标目录写成 app/storage，与源码布局一致，
        # 这样 app/paths.py 的 resolve_resource("storage", "schema.sql") 能对上。
        (str(PROJECT / "app" / "storage" / "schema.sql"), "app/storage"),
        # 程序图标（托盘 / 窗口标题栏 / 任务栏），resolve_resource("ico", "EDMS.ico")
        (str(PROJECT / "ico" / "EDMS.ico"), "ico"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

# ---------------------------------------------------------------------------
#  精简：PyInstaller 的 PySide6 hook 会把整个 Qt 拖进来，其中一大半用不到。
#  这里按文件名/插件目录精确剔除，实测省下约 33MB（93MB -> 60MB）。
#
#  判断依据都是"本应用确实不会走到那条路"，不是凭感觉：
#    opengl32sw.dll    纯 QWidget 应用用光栅绘制，从不创建 GL 上下文
#    libcrypto/libssl  只被 QtNetwork 依赖，而 QtNetwork 已在 EXCLUDES 里
#    Qt6Svg.dll        不加载任何 SVG
#    imageformats/*    只留 qico —— 程序图标是 .ico，没有 qico 插件
#                      QIcon 读不出来，托盘和标题栏就全空白（踩过一次）
#    iconengines/*     不按扩展名加载图标
#    qdirect2d.dll     备用平台插件，实际用的是 qwindows
#    qtuiotouchplugin  触屏输入，桌面机不需要
# ---------------------------------------------------------------------------
DROP_FILES = {
    "opengl32sw.dll",
    "libcrypto-3-x64.dll",
    "libssl-3-x64.dll",
    "Qt6Network.dll",
    "Qt6Svg.dll",
}
DROP_PLUGIN_SUBDIRS = {"imageformats", "iconengines"}
DROP_PLUGIN_FILES = {"platforms/qdirect2d.dll", "generic/qtuiotouchplugin.dll"}
# 在被剔除的目录里**单独放行**的 —— 程序图标是 .ico，解码全靠它
KEEP_PLUGIN_FILES = {"imageformats/qico.dll"}


def _keep(entry) -> bool:
    dest = entry[0].replace("\\", "/")
    name = dest.rsplit("/", 1)[-1]
    if name in DROP_FILES:
        return False
    if "plugins/" in dest:
        rel = dest.split("plugins/", 1)[1]
        if rel in KEEP_PLUGIN_FILES:
            return True        # 放行要放在目录级剔除之前
        if rel.split("/", 1)[0] in DROP_PLUGIN_SUBDIRS:
            return False
        if rel in DROP_PLUGIN_FILES:
            return False
    return True


_before = len(a.binaries) + len(a.datas)
a.binaries = [e for e in a.binaries if _keep(e)]
a.datas = [e for e in a.datas if _keep(e)]
print(f"[精简] 剔除 {_before - len(a.binaries) - len(a.datas)} 个用不到的二进制/插件")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EDMS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                   # UPX 压缩经常被杀软误报，自用工具不值当
    console=False,               # 不要黑框窗口；托盘 + GUI 才是正常形态
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 用户自己放的图标（ico/EDMS.ico）。.workbuddy/tools/make_icon.py 生成的是
    # 代码画的 build/icon.ico，已不再用于打包，工具留着当备手。
    icon=str(PROJECT / "ico" / "EDMS.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="EDMS",
)
