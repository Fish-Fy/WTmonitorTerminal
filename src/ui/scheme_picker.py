"""换图 / 重开相同地图时, 选方案或重命名的弹窗。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QHBoxLayout, QInputDialog, QMessageBox,
)

from ..i18n import map_name, tr
from ..store.models import Scheme


if TYPE_CHECKING:
    from .main_window import MainWindow


class SchemePicker(QDialog):
    def __init__(self, win: "MainWindow", scene: str, schemes: list[Scheme], parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("切换地图 / 选择方案"))
        self.setMinimumWidth(380)
        self.win = win
        self.scene = scene
        self.schemes = schemes
        self.selected: Scheme | None = None

        v = QVBoxLayout(self)
        v.addWidget(QLabel(tr('当前已切换到地图: <b>{0}</b>', map_name(scene))))

        self.lst = QListWidget()
        self._refresh()
        v.addWidget(self.lst, 1)

        row = QHBoxLayout()
        b_new = QPushButton(tr("新建方案")); b_new.clicked.connect(self._new)
        b_rename = QPushButton(tr("重命名场景")); b_rename.clicked.connect(self._rename_scene)
        b_open = QPushButton(tr("应用选中并打开")); b_open.clicked.connect(self._open)
        b_blank = QPushButton(tr("打开空白")); b_blank.clicked.connect(self._blank)
        row.addWidget(b_new); row.addWidget(b_rename); row.addWidget(b_open); row.addWidget(b_blank)
        v.addLayout(row)

    def _refresh(self):
        self.lst.clear()
        if not self.schemes:
            it = QListWidgetItem(tr("(无方案) - 点'新建方案'"))
            it.setFlags(it.flags() & ~Qt.ItemIsSelectable)
            self.lst.addItem(it)
        for s in self.schemes:
            n = len(s.markers)
            it = QListWidgetItem(tr('{0}   [{1} 个标点 · {2} 个区域]', s.name, n, len(s.areas)))
            it.setData(Qt.UserRole, s.id)
            self.lst.addItem(it)

    def _new(self):
        name, ok = QInputDialog.getText(self, tr("新建方案"), tr('为地图 [{0}] 命名:', map_name(self.scene)))
        if ok and name:
            self.win.create_scheme_for(self.scene, name)
            self.schemes = self.win._visible_schemes() if self.scene == self.win._scene else self.schemes
            self._refresh()

    def _rename_scene(self):
        new, ok = QInputDialog.getText(self, tr("重命名场景"), tr("把当前场景名改为:"), text=self.scene)
        if ok and new and new != self.scene:
            if self.win.rename_scene(self.scene, new):
                self.scene = new
                self.schemes = self.win._visible_schemes()
                self._refresh()

    def _open(self):
        it = self.lst.currentItem()
        if not it:
            QMessageBox.information(self, tr("提示"), tr("请选择一个方案。")); return
        sid = it.data(Qt.UserRole)
        s = next((x for x in self.schemes if x.id == sid), None)
        if not s:
            return
        self.selected = s
        self.win.choose_scheme_for(self.scene, s)
        self.accept()

    def _blank(self):
        self.selected = None
        self.win.choose_scheme_for(self.scene, None)
        self.accept()
