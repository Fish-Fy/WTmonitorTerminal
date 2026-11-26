"""威胁告警的自定义音效选择和试听。"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget,
)


from ..i18n import tr


class AlertPanel(QWidget):
    sound_file_changed = Signal(str)
    test_sound_requested = Signal()

    def __init__(self, sound_file: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 2, 4, 4)
        row.setSpacing(4)
        row.addWidget(QLabel(tr("告警音效")))
        self.sound_edit = QLineEdit(sound_file or "", self)
        self.sound_edit.setPlaceholderText(tr("留空静音"))
        row.addWidget(self.sound_edit, 1)
        b_browse = QPushButton(tr("浏览…"), self)
        b_browse.clicked.connect(self._browse)
        row.addWidget(b_browse)
        b_test = QPushButton(tr("试听"), self)
        b_test.clicked.connect(self._test)
        row.addWidget(b_test)
        self.sound_edit.setToolTip(tr("仅用于威胁告警"))
        self.sound_edit.editingFinished.connect(self._sound_edited)

    # ------------------------------------------------------------------ 内部
    def _browse(self) -> None:
        path, _ok = QFileDialog.getOpenFileName(
            self, tr("选择告警音效"), "", tr("音频文件 (*.wav *.mp3 *.ogg);;所有文件 (*)"))
        if path:
            self.sound_edit.setText(path)
            self.sound_file_changed.emit(path)

    def _test(self) -> None:
        self._sound_edited()                    # 先保证输入框里的路径已生效
        self.test_sound_requested.emit()

    def _sound_edited(self) -> None:
        self.sound_file_changed.emit(self.sound_edit.text().strip())

    # ------------------------------------------------------------------ 外部同步
    def set_sound_file(self, path: str) -> None:
        self.sound_edit.blockSignals(True)
        self.sound_edit.setText(path or "")
        self.sound_edit.blockSignals(False)
