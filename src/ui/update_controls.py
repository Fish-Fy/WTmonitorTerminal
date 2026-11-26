"""Background update checks and the user's download/install decisions."""
from __future__ import annotations

from queue import Empty, SimpleQueue
import shutil
from threading import Event, Thread
import time

import httpx
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QMenu, QMessageBox, QPushButton, QTextBrowser, QVBoxLayout,
)

from ..i18n import tr
from .. import updater


class ReleaseNotesView(QTextBrowser):
    def loadResource(self, resource_type, url):
        # Render Markdown from the release as text and formatting only.
        return None


def notes_dialog(window, title, summary, notes):
    dialog = QDialog(window)
    dialog.setWindowTitle(title)
    dialog.resize(580, 420)
    layout = QVBoxLayout(dialog)
    label = QLabel(summary)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    layout.addWidget(label)
    viewer = ReleaseNotesView(dialog)
    viewer.setOpenLinks(False)
    markdown = QTextDocument.MarkdownFeature
    viewer.document().setMarkdown(notes, markdown.MarkdownDialectGitHub | markdown.MarkdownNoHTML)
    layout.addWidget(viewer, 1)
    buttons = QDialogButtonBox(dialog)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    return dialog, buttons


class UpdateButton(QPushButton):
    def __init__(self, window, resources, version):
        super().__init__(tr("检查更新"), window)
        self.window = window
        self.resources = resources
        self.version = version
        self.phase = ""
        self.release = None
        self.work = None
        self.events = SimpleQueue()
        self.cancel = Event()
        self.setToolTip(tr('当前版本 {0} · 右键查看更新日志', version))
        self.clicked.connect(self._clicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._poll)
        QTimer.singleShot(6000, self._background_check)
        QTimer.singleShot(800, self._report_previous_failure)

    def _show_menu(self, position):
        menu = QMenu(self)
        menu.addAction(tr("更新日志…"), self._show_history)
        menu.exec(self.mapToGlobal(position))

    def _show_history(self):
        entries = updater.history_notes(self.window.data_root)
        if self.version not in entries:
            entries[self.version] = updater.read_notes(self.resources / updater.NOTES_FILE) or tr(updater.NO_NOTES)
        body = "\n\n---\n\n".join(
            f"## {version}\n\n{entries[version]}"
            for version in sorted(entries, key=updater.version_key, reverse=True)
        )
        dialog, buttons = notes_dialog(self.window, tr("更新日志"), tr('当前版本 {0}', self.version), body)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        dialog.exec()

    def report_installed(self, online_notes):
        notes = online_notes or updater.read_notes(self.resources / updater.NOTES_FILE) or tr(updater.NO_NOTES)
        summary = tr('已更新至 {0}。', self.version)
        try:
            updater.record_update(self.window.data_root, self.version, notes)
            summary += tr("可右键点击「检查更新」回看日志。")
        except OSError:
            summary += tr("本机日志保存失败。")
        dialog, buttons = notes_dialog(self.window, tr("更新完成"), summary, notes)
        buttons.addButton(QDialogButtonBox.StandardButton.Ok)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()

    def _background_check(self):
        if not self.phase and not self.release and not self.work:
            self._check(False)

    def _report_previous_failure(self):
        status = self.window.root / ".wt-update-error.txt"
        try:
            detail = status.read_text(encoding="utf-8")
            status.unlink()
        except OSError:
            return
        self._message(tr("上次更新未完成。"), detail)

    def _message(self, text, detail="", links=False, history=False):
        dialog = QMessageBox(QMessageBox.Icon.Information, tr("软件更新"), text, parent=self.window)
        dialog.addButton(QMessageBox.StandardButton.Ok)
        if detail:
            dialog.setDetailedText(detail)
        if links:
            github = dialog.addButton(tr("打开 GitHub"), QMessageBox.ButtonRole.ActionRole)
            quark = dialog.addButton(tr("打开夸克下载"), QMessageBox.ButtonRole.ActionRole)
        log = dialog.addButton(tr("更新日志"), QMessageBox.ButtonRole.ActionRole) if history else None
        dialog.exec()
        clicked = dialog.clickedButton()
        if log is not None and clicked == log:
            self._show_history()
        if links:
            if clicked in (github, quark):
                QDesktopServices.openUrl(QUrl(updater.RELEASE_URL if clicked == github else updater.QUARK_URL))

    def _clicked(self):
        if self.phase == "download":
            if QMessageBox.question(self.window, tr("软件更新"), tr("取消下载此次更新？")) == QMessageBox.StandardButton.Yes:
                self.cancel.set()
                self.setText(tr("正在取消…"))
        elif self.phase:
            return
        elif self.work:
            self._install()
        elif self.release:
            self._offer_download()
        else:
            self._check(True)

    def _start(self, phase, action, manual):
        self.phase = phase
        self.manual = manual
        self.cancel.clear()
        self.setText(tr("检查中…") if phase == "check" else tr("下载中…"))
        self.setEnabled(phase != "check")
        self.timer.start()

        def worker():
            try:
                with httpx.Client(timeout=12, headers={"User-Agent": f"WTMonitorTerminal/{self.version}"}) as client:
                    result = action(client)
                self.events.put(("result", result))
            except Exception as error:
                self.events.put(("error", error))

        Thread(target=worker, name="software-update", daemon=True).start()

    def _check(self, manual):
        self._start("check", lambda client: updater.check_latest(client, self.version), manual)

    def _offer_download(self):
        release = self.release
        size = release.size / 1024**2
        dialog, buttons = notes_dialog(
            self.window, tr("发现新版"),
            tr('{0} → {1}（约 {2:.0f} MB）\n更新会保留方案、地图和个人设置。', self.version, release.tag, size),
            release.notes or tr(updater.NO_NOTES),
        )
        buttons.addButton(tr("下载更新"), QDialogButtonBox.ButtonRole.AcceptRole)
        details = buttons.addButton(tr("打开发布页"), QDialogButtonBox.ButtonRole.ActionRole)
        details.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(release.page)))
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        root, data_root, release = self.window.root, self.window.data_root, self.release
        self._start("download", lambda client: updater.prepare(
            client, release, root, data_root, self.cancel,
            lambda seen, size: self.events.put(("progress", (seen, size)))), True)

    def _install(self):
        if QMessageBox.question(
            self.window, tr("软件更新"), tr('现在关闭终端并重启到 {0}？', self.release.tag),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.helper = updater.launch_installer(self.work, self.resources)
        except (OSError, ValueError) as error:
            self._message(tr("无法启动更新器。"), str(error), links=True)
            return
        self.phase = "install"
        self.deadline = time.monotonic() + 12
        self.setEnabled(False)
        self.setText(tr("准备重启…"))
        self.timer.start()

    def _poll(self):
        if self.phase == "install":
            if (self.work / "accepted").is_file():
                self.work = None
                self.phase = ""
                self.timer.stop()
                self.window.close()
            elif self.helper.poll() is not None or time.monotonic() > self.deadline:
                if self.helper.poll() is None:
                    self.helper.kill()
                (self.work / "accepted").unlink(missing_ok=True)
                self._idle()
                self._message(tr("更新器未能启动。请稍后重试，或手动下载更新。"), links=True)
            return
        while True:
            try:
                kind, value = self.events.get_nowait()
            except Empty:
                return
            if kind == "progress":
                seen, size = value
                if not self.cancel.is_set():
                    self.setText(tr('下载 {0}%', seen * 100 // size) if size else tr("正在验证…"))
                continue
            phase, manual = self.phase, self.manual
            if kind == "result" and phase == "check":
                self.release = value
                self._idle()
                if manual:
                    if value:
                        self._offer_download()
                    else:
                        self._message(tr('{0} 已是最新版。', self.version), history=True)
            elif kind == "result":
                self.work = value
                self._idle()
            else:
                self._idle()
                if manual and not self.cancel.is_set():
                    description = (str(value) if isinstance(value, updater.UpdateError) else
                                   tr("无法完成更新。请检查网络、磁盘空间或文件夹权限后重试。"))
                    self._message(description, str(value), links=True, history=phase == "check")
            return

    def _idle(self):
        self.phase = ""
        self.timer.stop()
        self.setEnabled(True)
        self.setText(tr("安装新版") if self.work else tr("有新版") if self.release else tr("检查更新"))

    def can_close(self):
        if self.phase == "install":
            return False
        if self.phase == "download":
            self.cancel.set()
            self.setText(tr("正在取消…"))
            return False
        if self.work:
            shutil.rmtree(self.work, ignore_errors=True)
            self.work = None
        return True
