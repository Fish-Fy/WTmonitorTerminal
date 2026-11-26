# 打包脚本: 把终端打成 dist\WTMonitorTerminal\WTMonitorTerminal.exe
#
# 用法(在项目根目录):
#   .\.venv\Scripts\python.exe -m PyInstaller wt_terminal.spec --noconfirm --clean
#
# 为什么用 onedir 而不是 onefile:
#   * onefile 每次启动都要把 ~150MB 解包到临时目录, 冷启动 5~10 秒, 而且 taskkill 容易留垃圾;
#   * onedir 里双击那个 exe 就行, 启动跟源码一样快, 出问题还能看到旁边的 dll。
#   想要单文件的话把 EXE(...) 里的 exclude_binaries/COLLECT 换成 onefile 写法即可。
#
# 注意: config.yaml 与 assets/ 是**外挂**在 exe 旁边的(不是塞进包里), 因为
#   data/(底图/方案/偏好) 必须写在 exe 同级目录才可写、可备份。

import os

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[
        ("config.example.yaml", "."),  # 仅打包公开默认配置
        ("LICENSE", "."),
        ("assets", "assets"),        # 图标(窗口/任务栏用)
        ("src/web/static", "src/web/static"),  # 手机浏览器页面
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 这些是项目没用到的大件, 排掉能省不少体积
    excludes=[
        "tkinter", "unittest", "pydoc_data", "test",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
        "PySide6.QtMultimediaWidgets", "PySide6.QtQuick", "PySide6.QtQml",
        "PySide6.QtBluetooth", "PySide6.QtNetworkAuth", "PySide6.QtPdf",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtSql",
        "PySide6.QtTest", "PySide6.QtSensors", "PySide6.QtSerialPort",
        "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGLWidgets",
        "numpy", "PIL",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WTMonitorTerminal",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                        # UPX 压缩容易被杀软误报, 个人自用就别压
    console=False,                    # 无控制台窗口(要看报错就用 启动终端.bat 跑源码)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/app.ico",            # exe 自己的图标
    version="assets/version_info.txt",  # 文件属性里的说明/版本/版权
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="WTMonitorTerminal",
)
