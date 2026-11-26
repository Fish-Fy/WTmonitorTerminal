"""可折叠面板外壳：一行标题 + 正文，点标题折/开。

为什么要它: 用户"平时不需要看击杀播报", 但又不希望隐藏后功能悄悄断掉 ——
所以折叠只影响**可见性**(body 隐藏), 里面装的控件照常收发数据。
标题右侧还能挂一个计数徽章(badge), 折起来也看得见"刚刚有多少条"。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QSizePolicy, QToolButton, QVBoxLayout, QWidget

_ARROW = {True: Qt.ArrowType.RightArrow, False: Qt.ArrowType.DownArrow}


class CollapsibleBox(QWidget):
    toggled = Signal(bool)        # 参数 = 现在是否折叠

    def __init__(self, title: str, parent: Optional[QWidget] = None, collapsed: bool = False):
        super().__init__(parent)
        self._title = title
        self._badge = ""
        self._collapsed = False
        self._tabbed = False

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        self.header = QToolButton()
        self.header.setText("")
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setArrowType(_ARROW[self._collapsed])
        self.header.setCheckable(False)
        sp = self.header.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
        self.header.setSizePolicy(sp)
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.clicked.connect(self._on_header)
        v.addWidget(self.header)

        self.body = QWidget(self)
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(2, 2, 2, 2)
        v.addWidget(self.body, 1)

        self.set_collapsed(collapsed, emit=False)

    # ------------------------------------------------------------------ 接口
    def set_body(self, widget: QWidget) -> None:
        widget.setParent(self.body)
        self._body_layout.addWidget(widget)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, value: bool, emit: bool = True) -> None:
        value = bool(value)
        changed = value != self._collapsed
        self._collapsed = value
        self._sync_body()
        self.header.setArrowType(_ARROW[value])
        self._refresh_text()
        if changed and emit:
            self.toggled.emit(value)

    def set_tabbed(self, active: bool) -> None:
        self._tabbed = active
        self.header.setVisible(not active)
        self._sync_body()

    def _sync_body(self) -> None:
        collapsed = self._collapsed and not self._tabbed
        self.body.setVisible(not collapsed)
        # 折叠时把最大高度收到标题高度 —— 否则面板仍占着原高度, 下面那块就是空白
        # (用户要求"折叠后自动向上对齐, 不要留空")。
        if collapsed:
            self.setMaximumHeight(self.header.sizeHint().height() + 6)
        else:
            self.setMaximumHeight(16777215)

    def toggle(self) -> None:
        self.set_collapsed(not self._collapsed)

    def set_badge(self, text: str) -> None:
        """计数徽章, 例如 "12 条"; 传空串清掉。"""
        text = (text or "").strip()
        if text == self._badge:
            return
        self._badge = text
        self._refresh_text()

    @property
    def title(self) -> str:
        return self._title

    # ------------------------------------------------------------------ 内部
    def _on_header(self) -> None:
        self.toggle()

    def _refresh_text(self) -> None:
        badge = f" · {self._badge}" if self._badge else ""
        self.header.setText(f"{self._title}{badge}")
