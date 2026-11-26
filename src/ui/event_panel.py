"""战场事件面板：按归属染色的击杀/伤害文本流。

行数由 maximumBlockCount 硬截断 —— 重连补收/刷屏时也不会把 UI 线程拖死。
第一人称改写与分类来自 src/agent/events.py，这里只管显示。
"""
from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QFrame, QPlainTextEdit, QWidget

# 分类 -> (颜色, 是否加粗, 前缀)
_STYLE: dict[str, tuple[str, bool, str]] = {
    "self_action": ("#b8860b", True, "★"),      # 我的战果
    "self_victim": ("#ff2d2d", True, "☠"),      # 我被击落
    "enemy_action": ("#c0392b", False, "▲"),    # 敌方行动(危险)
    "unknown": ("#8e8e93", False, "·"),         # 推不出阵营
}
_MAX_LINES = 400


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class EventPanel(QPlainTextEdit):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(_MAX_LINES)
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPointSize(9)
        self.setFont(f)
        self.setFrameShape(QFrame.Shape.NoFrame)

    # ------------------------------------------------------------------ public
    def add_items(self, items: Iterable) -> None:
        html: list[str] = []
        for it in items or ():
            color, bold, mark = _STYLE.get(getattr(it, "category", "unknown"), _STYLE["unknown"])
            t = float(getattr(it, "time", 0.0) or 0.0)
            stamp = f"{int(t // 60):02d}:{int(t % 60):02d}"
            body = _esc(getattr(it, "display", "") or getattr(it, "text", ""))
            weight = "font-weight:bold;" if bold else ""
            html.append(f'<div style="color:{color};{weight}">'
                        f'<span style="color:#7a7a7a">[{stamp}]</span> {mark} {body}</div>')
        if html:
            self.appendHtml("".join(html))

    def clear_feed(self) -> None:
        self.setPlainText("")

    def row_count(self) -> int:
        return self.blockCount()
