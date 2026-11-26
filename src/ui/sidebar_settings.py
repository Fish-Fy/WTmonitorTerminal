"""右侧栏内的设置页，开关始终从实际状态同步。"""
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from ..agent.fitting import ACTIVITY_FILL_RANGE, FOLLOW_ZOOM_RANGE
from ..agent.uiprefs import WINDOW_LAYOUTS
from ..i18n import LANGUAGES, tr
from .overlay import ITEM_LABELS


class SidebarSettings(QScrollArea):
    def __init__(self, win, section):
        super().__init__()
        self.win = win
        self.section = section
        self.checks = {}
        self.bindings = {}
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)
        titles = {"panels": tr("侧边栏面板"), "map": tr("地图显示"), "vehicle": tr("载具状态显示")}
        title = QLabel(titles[section])
        title.setProperty("pageTitle", True)
        layout.addWidget(title)
        if section == "vehicle":
            self.hint = QLabel()
            self.hint.setProperty("muted", True)
            layout.addWidget(self.hint)
        mv = win.map_view
        if section == "panels":
            display = self._group(layout, tr("窗口布局"))
            self.layout_combo = QComboBox()
            self.layout_combo.setAccessibleName(tr("窗口布局"))
            for key, label in WINDOW_LAYOUTS.items():
                self.layout_combo.addItem(tr(label), key)
            self.layout_combo.setCurrentIndex(self.layout_combo.findData(win.ui_prefs.window_layout))
            self.layout_combo.currentIndexChanged.connect(
                lambda: win.set_window_layout(self.layout_combo.currentData()))
            display.addWidget(self.layout_combo)
            languages = self._group(layout, tr("界面语言"), tr("重启终端后生效。"))
            self.language_combo = QComboBox()
            self.language_combo.setObjectName("interfaceLanguage")
            self.language_combo.setAccessibleName(tr("语言"))
            for code, label in LANGUAGES:
                self.language_combo.addItem(label, code)
            self.language_combo.setCurrentIndex(self.language_combo.findData(win.ui_prefs.language))
            self.language_combo.currentIndexChanged.connect(self._set_language)
            languages.addWidget(self.language_combo)
            combat = self._group(layout, tr("战斗提示"))
            self._check(combat, "rampage_alerts", tr("连杀提示"),
                        lambda: win.ui_prefs.rampage_alerts, win.set_rampage_alerts)
            self._check(combat, "nearby_death_popup", tr("附近队友阵亡弹窗"),
                        lambda: win.ui_prefs.nearby_death_popup, win.set_nearby_death_popup)
            self._check(combat, "combat_overlay_rows", tr("交战车型悬浮提示"),
                        lambda: win.ui_prefs.combat_overlay_rows, win.set_combat_overlay_rows)
            group = self._group(layout, tr("显示的面板"))
            for key, label, _visible in win.panel_menu_entries():
                self._check(group, key, label,
                            lambda k=key: win.ui_prefs.visible(k),
                            lambda on, k=key: win._set_panel_visible(k, on))
        elif section == "vehicle":
            group = self._group(layout, tr("姿态仪样式"))
            self.attitude_style = QComboBox()
            self.attitude_style.addItem(tr("地平线倾斜（原样式）"), False)
            self.attitude_style.addItem(tr("飞机倾斜（地平线水平）"), True)
            self.attitude_style.currentIndexChanged.connect(
                lambda _index: win.set_attitude_aircraft_tilt(self.attitude_style.currentData()))
            group.addWidget(self.attitude_style)
            for name, keys in ((tr("姿态与导航"), ("attitude", "heading", "aoa", "aos", "ny", "roll_rate")),
                               (tr("速度与高度"), ("ias", "tas", "mach", "alt", "vario")),
                               (tr("动力与连接"), ("throttle", "fuel", "conn"))):
                group = self._group(layout, name)
                for key in keys:
                    self._check(group, key, tr(ITEM_LABELS[key]), lambda k=key: mv.overlay.is_on(k),
                                lambda on, k=key: self._vehicle(k, on))
        else:
            group = self._group(layout, tr("战局信息"))
            self._check(group, "show_ticket_eta", tr("显示预计结束时间"),
                        lambda: win.ui_prefs.show_ticket_eta, win.set_ticket_eta_visible)

            group = self._group(layout, tr("视角"))
            self._check(group, "activity_fit", tr("自动放大地图"),
                        mv.activity_fit_enabled, lambda on: self._fit(on))
            self.activity_fill_spin = QSpinBox()
            self.activity_fill_spin.setRange(*(round(limit * 100) for limit in ACTIVITY_FILL_RANGE))
            self.activity_fill_spin.setSuffix("%")
            self.activity_fill_spin.setValue(round(mv.activity_fill * 100))
            self.activity_fill_spin.setToolTip(tr("任务范围占可视正方形边长的比例。数值越大，地图放得越近。"))
            self.activity_fill_spin.valueChanged.connect(lambda percent: win.set_activity_fill(percent / 100))
            self._field(group, tr("自动放大比例"), self.activity_fill_spin)
            self._check(group, "activity_edge_pan", tr("靠近边缘时平移"),
                        lambda: win.ui_prefs.activity_edge_pan, win.set_activity_edge_pan)
            self.checks["activity_edge_pan"].setToolTip(
                tr("自动放大地图时，玩家靠近视口边缘便平移地图，保持当前放大比例。"))

            self._check(group, "follow_player", tr("跟随玩家"), lambda: mv.settings.follow_player,
                        mv.set_follow_player)
            self.follow_zoom_spin = QDoubleSpinBox()
            self.follow_zoom_spin.setDecimals(1)
            self.follow_zoom_spin.setRange(*FOLLOW_ZOOM_RANGE)
            self.follow_zoom_spin.setSingleStep(0.1)
            self.follow_zoom_spin.setSuffix("×")
            self.follow_zoom_spin.setValue(mv.follow_zoom)
            self.follow_zoom_spin.setToolTip(tr("相对整图视角的放大倍数。1.0× 为整图大小。"))
            self.follow_zoom_spin.valueChanged.connect(win.set_follow_zoom)
            self._field(group, tr("跟随玩家放大比例"), self.follow_zoom_spin)

            whole = QPushButton(tr("查看整张地图"))
            whole.clicked.connect(mv.show_whole_map)
            group.addWidget(whole)

            group = self._group(layout, tr("警戒圈"))
            self._check(group, "guard_circle", tr("显示警戒圈"),
                        lambda: mv.settings.guard_circle, win._on_guard_circle)
            self.guard_ratio_spin = QSpinBox()
            self.guard_ratio_spin.setRange(1, 50)
            self.guard_ratio_spin.setSuffix("%")
            self.guard_ratio_spin.setValue(round(mv._guard_ratio * 100))
            self.guard_ratio_spin.setToolTip(tr("警戒半径占可视区边长的比例，同时用于附近队友阵亡告警。"))
            self.guard_ratio_spin.valueChanged.connect(lambda percent: win._on_guard_radius(percent / 100))
            self._field(group, tr("警戒比例"), self.guard_ratio_spin)

            group = self._group(layout, tr("轨迹与地图标记"))
            labels = {
                "player_trail": tr("玩家轨迹"),
                "teammate_trails": tr("队友轨迹"), "teammate_deaths": tr("队友死亡标记"),
                "own_deaths": tr("我被击落"),
            }
            for key, label in labels.items():
                self._check(group, key, label, lambda k=key: bool(getattr(mv.settings, k)),
                            lambda on, k=key: win.set_map_display_option(k, on))
            self._check(group, "teammate_heading", tr("队友方向环"),
                        lambda: mv.settings.teammate_heading, win.set_teammate_heading)
            clear = QPushButton(tr("清除轨迹与事件标记"))
            clear.clicked.connect(mv.clear_trail)
            layout.addWidget(clear)
        layout.addStretch(1)
        self.refresh()

    @staticmethod
    def _group(layout, title, hint=None):
        box = QGroupBox(title)
        inner = QVBoxLayout(box)
        inner.setContentsMargins(10, 18, 10, 10)
        inner.setSpacing(8)
        if hint:
            label = QLabel(hint)
            label.setWordWrap(True)
            label.setProperty("muted", True)
            inner.addWidget(label)
        layout.addWidget(box)
        return inner

    def _check(self, layout, key, label, getter, setter):
        check = QCheckBox(label)
        check.toggled.connect(setter)
        self.checks[key] = check
        self.bindings[key] = getter
        layout.addWidget(check)

    @staticmethod
    def _field(layout, title, editor):
        row = QHBoxLayout()
        label = QLabel(title)
        label.setBuddy(editor)
        editor.setAccessibleName(title)
        row.addWidget(label)
        row.addStretch(1)
        row.addWidget(editor)
        layout.addLayout(row)

    def refresh(self):
        if self.section == "vehicle":
            self.hint.setText(tr("已显示") if self.win.map_view.overlay.flight_active else tr("已隐藏 · 仅限飞机"))
            blocker = QSignalBlocker(self.attitude_style)
            self.attitude_style.setCurrentIndex(1 if self.win.map_view.overlay.aircraft_tilt else 0)
            del blocker
        for key, check in self.checks.items():
            blocker = QSignalBlocker(check)
            check.setChecked(bool(self.bindings[key]()))
            if key == "activity_fit":
                available = self.win.map_view.activity_fit_available()
                check.setEnabled(available)
                check.setToolTip("" if available else tr("驾驶飞机时停用"))
            del blocker

    def _vehicle(self, key, on):
        mv = self.win.map_view
        if mv.overlay.is_on(key) != on:
            mv._toggle_overlay(key)

    def _set_language(self):
        if not self.win.set_interface_language(self.language_combo.currentData()):
            with QSignalBlocker(self.language_combo):
                self.language_combo.setCurrentIndex(
                    self.language_combo.findData(self.win.ui_prefs.language))

    def _fit(self, on):
        mv = self.win.map_view
        if mv.activity_fit_enabled() != on:
            mv.toggle_activity_fit()
