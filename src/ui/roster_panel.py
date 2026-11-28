"""载具识别面板：按阵营分组列出每个人用过的载具，并区分存活/重生中/离场。

阵营不是接口给的 —— 是拿我的昵称从「甲 (载具) 击落了 乙 (载具)」这类文案里做二分图染色推出来的，
所以顶部会提示未配置昵称的情况。存活状态同理只用可证实的时间序证据（见 src/agent/roster.py 顶部）。

被击毁的人**不消失**（用户要求）：标记「重生中」、当前载具显示未知（死时那架已是历史，
明细子项里仍保留），等他重生后再次开火会自动翻回存活；只有明确离场（已掉线）才隐藏。
**未定阵营**按状态加分隔行（存活 / 重生中 / 离场 / 状态未知）—— 那一大坨人里，"刚刚还在开火的人"
和"已经被打下来的人"完全不是一个价值。分隔行与人是**同级**的，不再多套一层缩进（用户要求往前缩）。
勾选框只隐藏**已离场**的人（重生中保持可见）；偏好由主窗口存进 ui_state.json。
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPalette
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTreeWidget, QTreeWidgetItem,
    QLineEdit, QHeaderView, QCheckBox, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
)

from ..i18n import tr
from ..agent.mapobj import TEAM_COLORS
from ..agent.roster import STATUS_LABELS, STATUS_ORDER, TEAM_LABELS, short_name
from . import theme

_UNKNOWN = "unknown"
_SELF_NAME_COLOR = "#b8860b"
# 不在场上的人整体压暗, 让"还活着的"一眼能扫出来; 重生中比离场亮一档(他还会回来)
_DIM = {"killed": "#b0a08a", "left": "#7d6f96", "unknown": "#b0b0b5"}
_STATUS_ROLE = Qt.ItemDataRole.UserRole + 1
_KILLER_COLOR = "#ffbe55"
_KILLER_BACKGROUND = "#473321"


class StatusDelegate(QStyledItemDelegate):
    """统一绘制状态色，保留 Qt 的选中背景。"""

    def paint(self, painter, option, index):
        colored = index.data(_STATUS_ROLE)
        if colored is None:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        color, label, status_text = colored
        detail = opt.text[:-len(status_text)]
        style = opt.widget.style()
        area = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, opt.widget)
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        area.adjust(3, 0, -3, 0)
        fm = opt.fontMetrics
        # 空间不足时先缩短载具摘要，尽量保留完整状态和时间。
        status_space = fm.horizontalAdvance(status_text)
        if status_space > area.width():
            status_text = fm.elidedText(label, Qt.TextElideMode.ElideRight, max(0, area.width()))
            status_space = fm.horizontalAdvance(status_text)
        detail = fm.elidedText(detail, Qt.TextElideMode.ElideRight, max(0, area.width() - status_space))
        painter.save()
        painter.setClipRect(area)
        painter.setFont(opt.font)
        text_color = opt.palette.color(QPalette.ColorRole.Text)
        if opt.state & QStyle.StateFlag.State_Selected:
            text_color = QColor(theme.ACCENT)
        painter.setPen(text_color)
        painter.drawText(area, Qt.AlignmentFlag.AlignVCenter, detail)
        area.setLeft(area.left() + fm.horizontalAdvance(detail))
        painter.setPen(QColor(color))
        painter.drawText(area, Qt.AlignmentFlag.AlignVCenter, status_text)
        painter.restore()


class RosterPanel(QWidget):
    clear_requested = Signal()
    export_requested = Signal()
    alive_only_changed = Signal(bool)        # 主窗口据此把偏好写进 ui_state.json
    highlight_killers_changed = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._roster = None
        self._my_nick = ""
        self._alive_only = False
        self._highlight_killers = False
        self._hidden_left = 0
        self._portrait = False
        self._split_teams = False

        v = self._layout = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)

        self.hint = QLabel(tr("未设置游戏昵称"))
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color:{theme.HINT_WARN};")
        v.addWidget(self.hint)

        row = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(tr("过滤昵称/载具…"))
        self.filter_edit.textChanged.connect(self._refresh)
        row.addWidget(self.filter_edit, 1)
        b_export = QPushButton(tr("导出"))
        b_export.clicked.connect(self.export_requested.emit)
        b_clear = QPushButton(tr("清空"))
        b_clear.clicked.connect(self._on_clear)
        for b in (b_export, b_clear):
            row.addWidget(b)
        v.addLayout(row)

        row2 = QHBoxLayout()
        self.alive_chk = QCheckBox(tr("隐藏已离场"))
        self.alive_chk.setToolTip(tr("重生中的玩家仍显示"))
        self.alive_chk.toggled.connect(self._on_alive_toggled)
        row2.addWidget(self.alive_chk)
        self.highlight_killers_chk = QCheckBox(tr("高亮击毁过我的玩家"))
        self.highlight_killers_chk.setToolTip(tr("名字以亮橙色加粗显示，持续到本局结束"))
        self.highlight_killers_chk.toggled.connect(self._on_highlight_killers_toggled)
        row2.addWidget(self.highlight_killers_chk)
        row2.addStretch(1)
        v.addLayout(row2)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels([tr("昵称"), tr("击杀 · 载具 · 状态")])
        self.tree.header().setStretchLastSection(False)   # 默认 True 会把两列平均分掉, 固定宽度失效
        # 名字列: short_name 统一截到 24 个字符(**含战队前缀**) ⇒ 固定 150px 放得下,
        # 想更宽也能拖(Interactive); 余下宽度全给第二列(用户: "多留些空间显示载具名称")。
        self.tree.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 150)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(4)      # 子项缩进收窄(用户: 中间留空少一些)
        self.tree.itemClicked.connect(self._on_item_clicked)   # 点名字即展开该玩家的载具
        self.tree.setItemDelegateForColumn(1, StatusDelegate(self.tree))
        v.addWidget(self.tree, 1)

        self._teams = QWidget()
        teams_row = QHBoxLayout(self._teams)
        teams_row.setContentsMargins(0, 0, 0, 0)
        teams_row.setSpacing(6)
        self.team_trees = {}
        self.team_labels = {}
        for side in ("friend", "enemy"):
            column = QVBoxLayout()
            column.setSpacing(3)
            label = QLabel(tr(TEAM_LABELS[side]))
            label.setStyleSheet(f"color: {TEAM_COLORS[side]}; font-weight: bold; padding-left: 4px;")
            column.addWidget(label)
            team_tree = QTreeWidget()
            team_tree.setHeaderLabels([tr("昵称"), tr("载具"), tr("击杀"), tr("状态")])
            team_tree.setUniformRowHeights(True)
            team_tree.setIndentation(4)
            team_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            team_tree.itemClicked.connect(self._on_item_clicked)
            team_tree.setItemDelegateForColumn(3, StatusDelegate(team_tree))
            header = team_tree.header()
            header.setStretchLastSection(False)
            header.setMinimumSectionSize(24)
            for i in (0, 1):
                header.setSectionResizeMode(i, QHeaderView.Stretch)
            for i in (2, 3):
                header.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            column.addWidget(team_tree, 1)
            teams_row.addLayout(column, 1)
            self.team_trees[side] = team_tree
            self.team_labels[side] = label
        self._teams.hide()
        v.addWidget(self._teams, 1)

        footer = QHBoxLayout()
        self.count_lbl = QLabel(tr("0 人"))
        self.count_lbl.setWordWrap(True)
        footer.addWidget(self.count_lbl, 1)
        self.unknown_button = QPushButton()
        self.unknown_button.setCheckable(True)
        self.unknown_button.toggled.connect(self._sync_list_visibility)
        self.unknown_button.hide()
        footer.addWidget(self.unknown_button)
        v.addLayout(footer)

        self._map_counts: tuple[int, int] = (0, 0)     # 地图上的友/敌单位数(由主窗口喂)
        self._count_total = 0
        self._filter_empty = False

    # ------------------------------------------------------------------ public
    def set_portrait(self, active: bool) -> None:
        if self._portrait != active:
            self._portrait = active
            self._split_teams = self._can_split_teams()
            self._layout.setSpacing(4 if active else -1)
            self.hint.setVisible(not active)
            self._refresh()

    def _can_split_teams(self) -> bool:
        if not self._portrait:
            return False
        metrics = self.fontMetrics()
        status_width = max(metrics.horizontalAdvance(tr(label) + " 00:00") for label in STATUS_LABELS.values())
        # 每队还需给昵称、载具、击杀和树控件边距留空间。
        return self.width() >= 2 * (status_width + 126) + 14

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        split = self._can_split_teams()
        if self._split_teams != split:
            self._split_teams = split
            self._refresh()

    def set_roster(self, roster, my_nick: str = "") -> None:
        self._roster = roster
        self._my_nick = (my_nick or "").strip()
        if self._my_nick:
            self.hint.setText(tr('我：{0}', self._my_nick))
            self.hint.setToolTip(tr("根据本局击杀记录判断阵营与状态"))
            self.hint.setStyleSheet(f"color:{theme.HINT_OK};")
        else:
            self.hint.setText(tr("未设置游戏昵称"))
            self.hint.setToolTip(tr("点击右上方“设置昵称”后可判断敌友"))
            self.hint.setStyleSheet(f"color:{theme.HINT_WARN};")
        self._refresh()

    def alive_only(self) -> bool:
        return self._alive_only

    def set_alive_only(self, value: bool, persist: bool = False) -> None:
        value = bool(value)
        if value == self._alive_only:
            return
        self._alive_only = value
        self.alive_chk.blockSignals(True)
        self.alive_chk.setChecked(value)
        self.alive_chk.blockSignals(False)
        if persist:
            self.alive_only_changed.emit(value)
        self._refresh()

    def _on_alive_toggled(self, value: bool) -> None:
        self._alive_only = bool(value)
        self.alive_only_changed.emit(bool(value))
        self._refresh()

    def highlight_killers(self) -> bool:
        return self._highlight_killers

    def set_highlight_killers(self, value: bool) -> None:
        self._highlight_killers = bool(value)
        self.highlight_killers_chk.blockSignals(True)
        self.highlight_killers_chk.setChecked(self._highlight_killers)
        self.highlight_killers_chk.blockSignals(False)
        self._refresh()

    def _on_highlight_killers_toggled(self, value: bool) -> None:
        self._highlight_killers = bool(value)
        self.highlight_killers_changed.emit(self._highlight_killers)
        self._refresh()

    def _on_clear(self) -> None:
        self.clear_requested.emit()
        self._refresh()

    def _on_item_clicked(self, item, _col: int = 0) -> None:
        """点玩家名字 -> 向下展开它的载具明细(再点收起)。"""
        if item is not None and item.childCount():
            item.setExpanded(not item.isExpanded())

    def set_map_counts(self, friend: int = 0, enemy: int = 0) -> None:
        """地图上的友/敌单位数 —— 用来解释"识别到的人数"为什么和局内人数不一样。"""
        self._map_counts = (int(friend or 0), int(enemy or 0))
        self._update_label()

    def _update_label(self) -> None:
        if self._filter_empty:
            self.count_lbl.setText(tr("0 人（过滤无匹配）"))
            return
        f, e = self._map_counts
        txt = (tr('{0} 人（含重生中）· 已隐藏 {1} 人（离场）', self._count_total, self._hidden_left)
               if self._alive_only else tr('{0} 人（文案识别，含我）', self._count_total))
        if f or e:
            txt += tr(' · 地图 友 {0}/敌 {1}', f, e)
        self.count_lbl.setText(txt + tr("（仅本局）"))

    # ------------------------------------------------------------------ render
    def _keep(self, r, display: str, needle: str) -> bool:
        if not self._match(r, display, needle):
            return False
        # 只藏"明确离场"的; 重生中(被击毁)保持可见 —— 他还会回来(用户要求)
        if self._alive_only and r.status_of(display) == "left":
            return False
        return True

    def _row(self, r, display: str, side: str, *, separate: bool = False) -> QTreeWidgetItem:
        """默认列: 昵称 | 击杀数 · 当前载具 · 状态。

        按用户要求删掉了"×N 提及次数"（一条击杀链会连发四句，那个数字本来就虚高），
        击杀数只统计「击落/击毁/击沉」主语，各载具的明细竖向放进悬停。
        """
        st = r.stats(display)
        status = st.get("status", "unknown")
        # 重生中: 当前载具未知(死时那架是历史, 明细子项里还留着), 主列显示 ?
        veh = "?" if status == "killed" else (st.get("current_vehicle") or "?")
        count = st.get("kill_count", 0)
        status_label = tr(STATUS_LABELS.get(status, status))
        status_text = status_label
        if st.get("status_time"):
            t = float(st["status_time"])
            status_text += f" {int(t // 60):02d}:{int(t % 60):02d}"
        # 名字列截到 8 个字符(战队前缀不计), 分隔符也去空格 —— 省下的宽度全给载具名
        nick = short_name(display)
        color = theme.ROSTER_STATUS_COLORS.get(status, theme.DIM)
        if separate:
            item = QTreeWidgetItem([nick, veh, str(count), status_text])
            item.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
            status_column = 3
        else:
            item = QTreeWidgetItem([nick, "·".join((tr('击杀 {0}', count), veh, status_text))])
            status_column = 1
        item.setData(status_column, _STATUS_ROLE, (color, status_label, status_text))
        if separate:
            item.setForeground(status_column, _color(color))
        tip = f"{display} · {veh} · {status_text}"
        # 载具明细做成**行内子项**: 点名字即向下展开(用户要求), 不再靠悬停或加宽列表
        detail = r.kills_detail(display) if hasattr(r, "kills_detail") else []
        for vehicle, n in detail:
            child = QTreeWidgetItem(["", vehicle, str(n)]) if separate else QTreeWidgetItem(
                [vehicle, tr('击杀 {0}', n)])
            if separate:
                child.setTextAlignment(2, Qt.AlignmentFlag.AlignCenter)
            for col in range(child.columnCount()):
                child.setForeground(col, _color(theme.DIM))
                child.setToolTip(col, tr('{0} · 击杀 {1}', vehicle, n))
            item.addChild(child)
        item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        item.setExpanded(False)
        dim = _DIM.get(status)
        name_color = _SELF_NAME_COLOR if side == "me" else theme.TEXT
        item.setForeground(0, _color(dim or name_color))
        if dim and not separate:
            item.setForeground(1, _color(dim))
        # killed_me 累计保留于本局 Roster，不随阵亡、重生或更换载具清除。
        if self._highlight_killers and side != "me" and st.get("killed_me"):
            item.setForeground(0, _color(_KILLER_COLOR))
            item.setBackground(0, _color(_KILLER_BACKGROUND))
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            tip = tr('{0}\n本局击毁过你 {1} 次', tip, st['killed_me'])
        for col in range(item.columnCount()):
            item.setToolTip(col, tip)
        return item

    def _match(self, r, display: str, needle: str) -> bool:
        if not needle:
            return True
        return needle in display.lower() or needle in " ".join(r.vehicles(display)).lower()

    def _bold_root(self, title: str) -> QTreeWidgetItem:
        root = QTreeWidgetItem([title, ""])
        root.setFlags(root.flags() & ~Qt.ItemIsSelectable)
        f = root.font(0)
        f.setBold(True)
        root.setFont(0, f)
        return root

    def _refresh(self) -> None:
        r = self._roster
        self.tree.clear()
        for team_tree in self.team_trees.values():
            team_tree.clear()
        self._hidden_left = 0
        if r is None:
            self.count_lbl.setText(tr("0 人"))
            self._sync_list_visibility()
            return
        needle = self.filter_edit.text().strip().lower()
        names = r.display_names() if hasattr(r, "display_names") else r.names()
        displayed = set(names)
        my_names = set(r.roster("me"))
        total = 0
        for side, title in TEAM_LABELS.items():
            candidates = list(r.roster(side))
            if side == "friend":
                candidates.extend(my_names)
            rows = [d for d in candidates if d in displayed and self._keep(r, d, needle)]
            team_title = f"{tr(title)} ({len(rows)})"
            if self._split_teams:
                self.team_labels[side].setText(team_title)
            if not rows:
                continue
            total += len(rows)
            root = None
            if not self._split_teams:
                root = self._bold_root(team_title)
                root.setForeground(0, _color(TEAM_COLORS[side]))
                self.tree.addTopLevelItem(root)
            # 组内按状态靠拢(用户 2026-10-13 "排版改为存活已知的靠在一起, 未知靠在一起, 离场放一起"):
            # alive/killed 都算"状态已知且还在场"(killed 显示为"重生中", 面板里不消失) ⇒ 同一组;
            # 状态未知的居中; 已离场的垫底。组内仍按名字排, 便于找特定的人。
            order = ("alive", "killed", "unknown", "left")

            def _rank(name: str, _r=r, _o=order) -> tuple:
                st = _r.status_of(name)
                gi = _o.index(st) if st in _o else len(_o)
                try:                                  # 组内击杀数降序(用户 2026-10-13)
                    k = int(_r.stats(name).get("kill_count") or 0)
                except (TypeError, ValueError):
                    k = 0
                return (gi, -k, name.lower())         # 同击杀数再按名字, 顺序才稳定

            for d in sorted(rows, key=_rank):
                item = self._row(r, d, "me" if d in my_names else side, separate=self._split_teams)
                if root is None:
                    self.team_trees[side].addTopLevelItem(item)
                else:
                    root.addChild(item)
            if root is not None:
                root.setExpanded(True)

        # 未定阵营: 按存活状态加分隔行(同级, 不再多缩一级)
        groups = r.by_status(_UNKNOWN)
        shown = {s: [d for d in g if self._keep(r, d, needle)] for s, g in groups.items()}
        shown = {s: v for s, v in shown.items() if v}
        unknown_count = sum(len(group) for group in shown.values())
        if unknown_count:
            total += unknown_count
            root = self._bold_root(tr('未定阵营 ({0})', unknown_count))
            self.tree.addTopLevelItem(root)
            for status in STATUS_ORDER:
                rows = shown.get(status)
                if not rows:
                    continue
                if len(shown) > 1:                  # 只剩一组时分隔行没意义; 存活+重生中并存时要留
                    head = QTreeWidgetItem([f"— {tr(STATUS_LABELS[status])} ({len(rows)}) —", ""])
                    head.setFlags(head.flags() & ~Qt.ItemIsSelectable)
                    head.setForeground(0, _color("#9a9a9e"))
                    root.addChild(head)
                for d in sorted(rows, key=str.lower):
                    root.addChild(self._row(r, d, _UNKNOWN))
            root.setExpanded(True)

        self._count_total = total
        if self._alive_only:
            self._hidden_left = sum(1 for n in names if r.status_of(n) == "left")
        self._filter_empty = bool(needle and total == 0)
        # 隐藏离场时, 把"被藏起来的人"明写在列表末尾 —— 否则下方一片空白, 看着像"显示不全"
        if not self._split_teams and self._hidden_left > 0:
            tip = QTreeWidgetItem([tr('— 已隐藏 {0} 人（已离场，取消勾选可见）—', self._hidden_left), ""])
            tip.setFlags(tip.flags() & ~Qt.ItemIsSelectable)
            tip.setForeground(0, _color("#7d6f96"))
            self.tree.addTopLevelItem(tip)
        self.unknown_button.setText(tr('未定阵营 ({0})', unknown_count))
        self.unknown_button.setVisible(self._split_teams and unknown_count > 0)
        if not unknown_count:
            self.unknown_button.setChecked(False)
        self._sync_list_visibility()
        self._update_label()

    def _sync_list_visibility(self, _checked=False) -> None:
        teams = self._split_teams and not self.unknown_button.isChecked()
        self._teams.setVisible(teams)
        self.tree.setVisible(not teams)

    def hidden_left(self) -> int:
        """勾选"隐藏已离场"时被藏起来的人数(只统计 left; 重生中不藏)。"""
        return self._hidden_left


def _color(hex_str: str) -> QBrush:
    return QBrush(QColor(hex_str))
