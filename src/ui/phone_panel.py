"""侧栏手机连接入口。"""
import sys

from PySide6.QtCore import QProcess, QSignalBlocker
from PySide6.QtWidgets import (QApplication, QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QSpinBox, QVBoxLayout, QWidget)
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl

from ..i18n import tr
from ..web.firewall import repair_command


class PhonePanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self.mirror = window.phone_mirror
        self._repair_process = None
        self._repair_port = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        title = QLabel(tr("手机浏览器显示"))
        title.setProperty("pageTitle", True)
        layout.addWidget(title)
        hint = QLabel(tr("连接同一 Wi-Fi 或电脑热点，在手机浏览器打开下方地址。"))
        hint.setWordWrap(True)
        hint.setProperty("muted", True)
        layout.addWidget(hint)
        self.enable = QCheckBox(tr("开启手机显示"))
        self.enable.toggled.connect(self._toggle)
        layout.addWidget(self.enable)
        row = QHBoxLayout()
        row.addWidget(QLabel(tr("端口")))
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(window.cfg.web.port or 8321)
        row.addWidget(self.port)
        row.addStretch(1)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.addresses = QPlainTextEdit()
        self.addresses.setReadOnly(True)
        self.addresses.setMaximumHeight(120)
        self.addresses.setPlaceholderText(tr("开启后显示手机访问地址"))
        layout.addWidget(self.addresses)
        row = QHBoxLayout()
        self.copy = QPushButton(tr("复制手机地址"))
        self.copy.clicked.connect(self._copy)
        self.preview = QPushButton(tr("本机预览"))
        self.preview.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(self.mirror.local_url)))
        row.addWidget(self.copy)
        row.addWidget(self.preview)
        layout.addLayout(row)
        refresh = QPushButton(tr("刷新网络地址"))
        refresh.clicked.connect(self.refresh)
        layout.addWidget(refresh)
        self.repair = QPushButton(tr("修复热点连接（管理员）"))
        self.repair.clicked.connect(self._repair_hotspot)
        layout.addWidget(self.repair)
        self.repair_status = QLabel(tr("手机连接电脑热点时，使用 192.168.137.1 的地址。打不开可点击修复，并允许 Windows 管理员确认。"))
        self.repair_status.setWordWrap(True)
        self.repair_status.setProperty("muted", True)
        layout.addWidget(self.repair_status)
        self.repair.setToolTip(tr("允许 192.168.137.0/24 热点网段访问当前手机显示 TCP 端口；规则会保留，重复点击可修复。"))
        self.addresses.setToolTip(tr("无法连接时，请检查专用网络防火墙和路由器设备隔离。"))
        layout.addStretch(1)
        self.mirror.changed.connect(self.refresh)
        self.refresh()

    def _toggle(self, on):
        if on:
            self.mirror.start(self.port.value())
        else:
            self.mirror.stop()
        self.refresh()

    def _copy(self):
        urls = self.mirror.urls()
        if urls:
            QApplication.clipboard().setText(urls[0])
            self.status.setText(tr("地址已复制"))

    def _repair_hotspot(self):
        if self._repair_process is not None or not self.mirror.running or sys.platform != "win32":
            return
        self._repair_port = self.mirror.server.bound_port
        program, arguments = repair_command(self._repair_port)
        process = QProcess(self)
        self._repair_process = process
        process.finished.connect(self._repair_finished)
        process.errorOccurred.connect(self._repair_error)
        self.repair_status.setText(tr("等待 Windows 管理员确认并配置防火墙…"))
        self.refresh()
        process.start(program, arguments)

    def _repair_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._repair_done(tr("无法启动 Windows PowerShell，防火墙修复未执行。"))

    def _repair_finished(self, code, status):
        if status != QProcess.ExitStatus.NormalExit:
            message = tr("防火墙修复异常结束，请重试。")
        elif code == 0:
            message = tr('热点防火墙已修复（端口 {0}）。请在手机刷新 192.168.137.1 的地址。', self._repair_port)
        elif code == 1223:
            message = tr("已取消管理员确认，防火墙未修改。可再次点击修复。")
        else:
            message = tr("防火墙修复失败，请重试；若电脑受单位策略管理，请联系管理员。")
        self._repair_done(message)

    def _repair_done(self, message):
        process = self._repair_process
        if process is None:
            return
        self._repair_process = None
        process.deleteLater()
        self.repair_status.setText(message)
        self.refresh()

    def refresh(self):
        running = self.mirror.running
        blocker = QSignalBlocker(self.enable)
        self.enable.setChecked(running)
        del blocker
        urls = self.mirror.urls()
        self.port.setEnabled(not running)
        self.enable.setEnabled(self._repair_process is None)
        self.repair.setEnabled(running and sys.platform == "win32" and self._repair_process is None)
        self.copy.setEnabled(bool(urls))
        self.preview.setEnabled(running)
        self.addresses.setPlainText("\n".join(urls))
        self.status.setText(self.mirror.error or (tr("已开启") if urls else
                            tr("已开启，但未检测到局域网地址；请检查电脑网络或 config.yaml 的 web.host") if running else
                            tr("当前未开启")))
