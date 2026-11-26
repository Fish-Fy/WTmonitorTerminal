"""War Thunder 战术小地图终端 - 入口。"""
import shutil
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication


def app_root() -> Path:
    """程序根目录: 源码运行时是 main.py 所在目录, 打包成 exe 后是 **exe 所在目录**。

    冻结后 `__file__` 指向 PyInstaller 的临时解包目录(_MEIPASS), 那里只读且每次重启都变 ——
    config.yaml 与 data/(底图/方案/偏好) 必须跟着 exe 放在一起才写得进去、才带得走。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_root() -> Path:
    """**只读资源**目录(图标等): 打包后在 _MEIPASS 里; 源码运行时就是项目根。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else ROOT


def ensure_config() -> Path:
    """保证 exe 旁边有一份可编辑的 config.yaml: 首次运行从包内拷出来。"""
    cfg = ROOT / "config.yaml"
    if not cfg.is_file():
        bundled = resource_root() / "config.yaml"
        if not bundled.is_file() and bundled != cfg:
            bundled = resource_root() / "config.example.yaml"
        if bundled.is_file() and bundled != cfg:
            try:
                shutil.copyfile(bundled, cfg)
            except OSError:
                pass                      # 拷不出来就用包内那份(只读), 不阻断启动
    return cfg if cfg.is_file() else resource_root() / "config.yaml"


ROOT = app_root()
sys.path.insert(0, str(ROOT))

from src.ui.main_window import MainWindow
from src.ui.theme import app_qss
from src.config import load_config
from src.agent.bundled_maps import install_sun_city
from src.updater import mark_started, read_version


def main() -> int:
    cfg = load_config(ensure_config())
    app = QApplication(sys.argv)
    app.setApplicationName("WTMonitorTerminal")
    _icon = resource_root() / "assets" / "app.ico"
    if not _icon.is_file():
        _icon = ROOT / "assets" / "app.ico"
    if _icon.is_file():                    # 窗口/任务栏图标(与 exe 图标同一个文件)
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(_icon)))
    app.setStyleSheet(app_qss())          # 战术深色 HUD(用户 2026-10-07 定稿), 只动外壳
    resources = resource_root()
    install_sun_city(ROOT / cfg.data_dir, resources)
    version = read_version(resources) if getattr(sys, "frozen", False) else None
    win = MainWindow(cfg, root=ROOT, update_resources=resources, update_version=version)
    app.aboutToQuit.connect(win.phone_mirror.stop)
    win.show()
    from PySide6.QtCore import QTimer
    def notify_started():
        notes = mark_started()
        if notes is not None and win.update_button is not None:
            win.update_button.report_installed(notes)

    QTimer.singleShot(0, notify_started)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
