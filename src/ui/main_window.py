"""主窗口：地图与工作区布局，启动及停止后台轮询。"""
from __future__ import annotations

from collections import deque
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from math import isfinite
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import QItemSelectionModel, Qt, QTimer, QSignalBlocker
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWidgets import (
    QWidget, QSplitter, QHBoxLayout, QVBoxLayout, QLabel,
    QComboBox, QPushButton, QMessageBox, QListWidget, QListWidgetItem,
    QCheckBox, QMenu, QAbstractItemView, QLineEdit, QInputDialog, QStackedWidget, QScrollArea, QSizePolicy,
    QDialog, QDialogButtonBox, QApplication, QGridLayout, QTabBar,
)

from ..config import Config, save_game_nickname
from ..i18n import language, map_name, normalize_language, set_language, tr
from ..agent.poller import Poller
from ..agent import mapimg
from ..agent import mapstatics
from ..agent import task_layout
from ..agent.mapinfo import MapInfo
from ..agent.roster import CombatEvent, Roster, parse_line, normalize_key
from ..agent.battle import BattleWatcher
from ..agent.tickets import TicketEstimate
from ..agent.killcorr import KillCorrelator
from ..agent.threatwatch import NearbyDeathNotice, NearbyDeathWatch, RampageNotice, RampageWatch
from ..agent.events import build_feed
from ..agent.alerts import AlertEngine
from ..agent.telemetry import parse_state
from ..agent.uiprefs import LABELS as PANEL_LABELS, UiPrefs, WINDOW_LAYOUTS
from ..store.areas import AREA_TYPES
from ..store.models import MARKER_TYPES, Scheme
from ..store import markers as markers_store
from .collapsible import CollapsibleBox
from .area_items import AreaItem
from .map_view import MapView, MarkerItem
from .roster_panel import RosterPanel
from .event_panel import EventPanel
from .alert_panel import AlertPanel
from .combat_overlay import CombatOverlay
from .sfx import Sfx
from .window_chrome import FramelessWindow
from .nav_icons import NavButton
from .task_filter import mission_name, task_choice_name, task_mode_combo


log = logging.getLogger(__name__)

# 没有实时地图对象(游戏在部署/阵亡界面时 `/map_obj.json` 返回 0 字节)时,
# 上一帧最多再"淡显"保留这么多秒, 超过就只留缓存的占领区/出生点 —— 过期敌人图标不能当情报用。
STALE_HOLD_SEC = 60.0
LIVE_FRAME_SEC = 2.0                      # 超过多个轮询周期没收到对象，不再标成实时


class ElidedLabel(QLabel):
    """单行名称省略显示，悬停可查看全文。"""
    def __init__(self, text=""):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.set_title(text)

    def set_title(self, text):
        self._full_text = text
        self.setToolTip(text)
        self.setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, self.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.set_title(self._full_text)


class MainWindow(FramelessWindow):
    def __init__(self, cfg: Config, root: Path, parent=None, *, update_resources=None, update_version=None):
        data_root = root / cfg.data_dir
        ui_prefs = UiPrefs.load(data_root)
        set_language(ui_prefs.language)
        super().__init__(parent)
        self.cfg = cfg
        self.root = root
        self.update_button = None
        if update_resources and update_version:
            from .update_controls import UpdateButton
            self.update_button = UpdateButton(self, update_resources, update_version)
        self.data_root = data_root
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._library_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="map-library")
        self._library_cancel = Event()
        self._library_future = None
        self._library_clean = None
        self._library_notify = False
        self._library_timer = QTimer(self)
        self._library_timer.setInterval(100)
        self._library_timer.timeout.connect(self._finish_library_scan)

        # 载具识别: **只在本局有效**（用户要求去掉跨局累积与落盘）; 阵营靠昵称从击杀文案反推
        self.roster_dir = self.data_root / "roster"     # 只给面板"导出"按钮当输出目录
        self.roster = Roster(my_nick=cfg.identity.my_nick)
        self._roster_dirty = False

        # 事件路由: 跨批次按 id 去重; 告警引擎用系统提示音(不引额外依赖)
        self._feed_seen: set[int] = set()
        self.alerts = AlertEngine(cfg.alerts)

        self._scene: str | None = None
        self._schemes: list[Scheme] = []
        self._task = None
        self._game_task = None
        self._task_candidate = None
        self._awaiting_battle_map = False
        self._task_catalog_scene = None
        self._task_catalog = []
        self._undo_history = deque(maxlen=50)
        self._shown_schemes = set()
        self._known_scheme_ids: set[str] = set()
        self._current: Scheme | None = None
        self._map_info: MapInfo | None = None

        # 界面偏好(右列面板显隐/折叠)。只存 6 个布尔值 —— 与"载具识别不落盘"是两码事。
        self.ui_prefs = ui_prefs
        self.panels: dict[str, CollapsibleBox] = {}
        self._portrait = False
        self._landscape_sidebar_fraction = 0.28
        self._portrait_map_fraction = 0.64
        # 对局边界(载具识别池"每一局清一次"的触发器, 见 src/agent/battle.py)
        self.battle = BattleWatcher()
        self._clock = time.monotonic            # 本地时钟(测试可替换)
        self._tickets = TicketEstimate(freshness=LIVE_FRAME_SEC)
        self._ticket_battle_active = False
        self._ticket_mission_finished = False
        self._ticket_scene = None
        # 击杀文案 ↔ 地图图标消失关联(敌我判定补强): 帧史/锚点/延迟判定, 全部本局内存
        self._killcorr = KillCorrelator()
        self._pending_kills: list[tuple[Any, float, int]] = []   # (解析后击杀事件, 局内秒, 已判次数)
        self._corr_seen: set[tuple[str, str, float]] = set()     # 同一次击杀的重复文案只判一次
        # 威胁告警(用户 2026-09-29 定稿): 附近队友连死 + 同一人连续击杀(绕后/狙台)
        # 半径: 面板里调过(ui_state.json)优先, 否则用 config.yaml; 时钟由调用处显式传 now
        ac = cfg.alerts
        self._nearby = NearbyDeathWatch(radius=ac.nearby_radius,
                                        count=ac.nearby_count, window=ac.nearby_window)
        self._rampage = RampageWatch(gap=ac.rampage_gap, count=ac.rampage_count)
        # 自定义音效(只服务威胁告警; 空路径=静音; 发声 API 全项目只许在 sfx.py)
        self._sfx = Sfx(ac.sound_file, parent=self)
        # 独立置顶透明小窗: 交战车型速览 + 告警横幅(浮在游戏画面右侧, 点击穿透)
        oc = cfg.combat_overlay
        self._combat_overlay = CombatOverlay(ttl=oc.ttl, max_rows=oc.max_rows) if oc.enabled else None
        self.clear_log: list[str] = []          # 每次清空的原因留痕(悬案终结器)
        self._fit_pending = False             # 换图后等第一帧完整对象再做可活动区域自适应
        self._veh_kind = "unknown"            # 我在开的载体类型: air/ground/other/unknown
        self._map_locked = False              # 手动选图(离线规划)后锁住, 不再跟随游戏地图
        self._library_mode = False
        self._game_scene = None
        self._game_image = None
        self._display_image_scene = None      # 禁止拿上一张图的底图执行新任务缩放
        self._game_objects = []
        self._game_objects_scene = None
        self._game_objects_at = -1e9
        self._cache_display_scene = None
        self._statics_fp: dict[str, str] = {}   # 场景 -> 静态要素指纹(避免每帧写盘)
        self._last_live_at: float = -1e9        # 上一次真正拿到地图对象的时刻(过期保留用)

        self.setWindowTitle(tr("War Thunder 战术终端"))
        screen = self.screen().availableGeometry()
        if ui_prefs.window_layout == "portrait" or (
                ui_prefs.window_layout == "auto" and screen.height() > screen.width()):
            self.resize(min(900, screen.width(), round(screen.height() * 0.60)),
                        min(1600, screen.height()))
        else:
            self.resize(1280, 800)

        self._build_ui()
        self.map_view.setFocus()
        self._wire()
        self._apply_ui_prefs()
        self._sync_window_layout()

        self.poller = Poller(cfg, self.data_root, self)
        # 节流: map_changed 高频, 仅在线程外用 QTimer 切换”最新图像“避免每帧重建场景
        # 这里直接连接, QGraphicsScene 更新底图 pixmap 成本可接受
        self.poller.connection_changed.connect(self._on_connection)
        self.poller.state_changed.connect(self._on_state)
        self.poller.indicators_changed.connect(self._on_indicators)
        self.poller.map_changed.connect(self._on_map)
        self.poller.scene_changed.connect(self._on_scene)
        self.poller.map_info_changed.connect(self._on_map_info)
        self.poller.map_objects_changed.connect(self._on_map_objects)
        self.poller.hud_events_changed.connect(self._on_hud_events)
        self.poller.battle_state_changed.connect(self._on_battle_state)
        self.poller.mission_status_changed.connect(self._on_mission_status)
        self.poller.start()
        # 先给实时地图和单位让出磁盘与 CPU，离线启动也会自动整理。
        self._startup_library_timer = QTimer(self)
        self._startup_library_timer.setSingleShot(True)
        self._startup_library_timer.timeout.connect(self.maintain_map_library)
        self._startup_library_timer.start(3000)

        self.roster_panel.set_roster(self.roster, cfg.identity.my_nick)
        self.roster_panel.export_requested.connect(self._export_roster)
        self.roster_panel.clear_requested.connect(self._clear_roster)
        self.roster_panel.alive_only_changed.connect(self._on_alive_only)
        self.roster_panel.highlight_killers_changed.connect(self._on_highlight_killers)
        # 威胁告警面板 ↔ 检测器/地图/音效 的接线(半径改动即生效并持久化)
        self._wire_threat_alerts()
        # 悬浮窗: show 在主窗口之后, 位置贴屏幕右缘(或 config 指定)
        if self._combat_overlay is not None:
            self._combat_overlay.show()
            self._combat_overlay.place_default(cfg.combat_overlay.x, cfg.combat_overlay.y)
        # 面板 1s 刷一次就够: 战斗文案每秒都在来, 每次事件都重建树会吃掉 UI 线程
        self._roster_timer = QTimer(self)
        self._roster_timer.setInterval(1000)
        self._roster_timer.timeout.connect(self._tick_roster)
        self._roster_timer.timeout.connect(self._expire_game_objects)
        self._roster_timer.timeout.connect(self._refresh_context)
        self._roster_timer.start()

        from ..web.desktop import PhoneMirror
        self.phone_mirror = PhoneMirror(self)
        self.phone_mirror.changed.connect(self._refresh_context)
        if cfg.web.enabled:
            self.phone_mirror.start()

        QTimer.singleShot(0, self._maybe_show_unconnected)
        self._refresh_context()

    def edit_game_nickname(self) -> None:
        nickname, accepted = QInputDialog.getText(
            self, tr("游戏昵称"), tr("填写自己的游戏昵称（与游戏内一致）："), text=self.cfg.identity.my_nick)
        if not accepted:
            return
        nickname = nickname.strip()
        if not nickname:
            QMessageBox.warning(self, tr("游戏昵称"), tr("请填写自己的游戏昵称。"))
            return
        if nickname == self.cfg.identity.my_nick:
            return
        try:
            save_game_nickname(self.root / "config.yaml", nickname)
        except Exception as error:
            QMessageBox.warning(self, tr("保存失败"), tr('昵称未保存：{0}', error))
            return
        self.cfg.identity.my_nick = nickname
        self.roster.set_my_nick(nickname)
        self.roster_panel.set_roster(self.roster, nickname)
        self._roster_dirty = False
        self.nickname_button.setText(tr("修改昵称"))
        self.nickname_label.set_title(tr('我：{0}', self.roster.self_display_name()))
        self._update_badges()
        self.statusBar().showMessage(tr("游戏昵称已保存并生效"), 5000)

    # ---------------- UI build ----------------
    def _build_ui(self):
        central = QWidget()
        outer = self.workspace_layout = QVBoxLayout(central)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        split = QSplitter(Qt.Horizontal)
        self.split = split

        self.map_view = MapView(self)
        split.addWidget(self.map_view)

        right = QWidget()
        right_layout = self.sidebar_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.setSpacing(8)
        right.setMinimumWidth(326)
        split.setChildrenCollapsible(False)
        context = self.workspace_context = QWidget()
        context.setObjectName("sidebarContext")
        context_layout = QVBoxLayout(context)
        context_layout.setContentsMargins(10, 8, 10, 8)
        context_layout.setSpacing(4)
        heading = QHBoxLayout()
        self.map_title = ElidedLabel(tr("等待地图"))
        self.map_title.setObjectName("mapTitle")
        self.context_badge = QLabel(tr("等待游戏"))
        self.context_badge.setObjectName("contextBadge")
        heading.addWidget(self.map_title, 1)
        heading.addWidget(self.context_badge)
        context_layout.addLayout(heading)
        identity_row = QHBoxLayout()
        self.nickname_label = ElidedLabel()
        self.nickname_label.setObjectName("nicknameLabel")
        self.nickname_label.set_title(tr('我：{0}', self.cfg.identity.my_nick) if self.cfg.identity.my_nick else tr("未设置游戏昵称"))
        identity_row.addWidget(self.nickname_label, 1)
        self.nickname_button = QPushButton(tr("修改昵称") if self.cfg.identity.my_nick else tr("设置昵称"))
        self.nickname_button.setToolTip(tr("设置自己的游戏昵称，用于本局敌友识别"))
        self.nickname_button.clicked.connect(self.edit_game_nickname)
        identity_row.addWidget(self.nickname_button)
        if self.update_button is not None:
            identity_row.addWidget(self.update_button)
        context_layout.addLayout(identity_row)
        self.context_hint = QLabel(tr("等待地图"))
        self.context_hint.setObjectName("contextHint")
        self.context_hint.setWordWrap(True)
        context_layout.addWidget(self.context_hint)
        right_layout.addWidget(context)
        navigation = QHBoxLayout()
        navigation.setSpacing(5)
        self.sidebar_buttons = {}
        for key, title in (("panels", tr("面板设置")), ("map", tr("地图显示")),
                           ("vehicle", tr("载具状态显示")), ("library", tr("地图库")), ("phone", tr("手机显示"))):
            button = NavButton(key, title)
            button.clicked.connect(lambda _checked=False, k=key: self._toggle_sidebar(k))
            navigation.addWidget(button, 1)
            self.sidebar_buttons[key] = button
        self.sidebar_back = NavButton("back", tr("当前战况"))
        self.sidebar_back.setCheckable(False)
        self.sidebar_back.clicked.connect(self._back_to_workspace)
        navigation.addWidget(self.sidebar_back, 1)
        right_layout.addLayout(navigation)
        self.workspace_tabs = QTabBar()
        self.workspace_tabs.setObjectName("workspaceTabs")
        self.workspace_tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.workspace_tabs.setUsesScrollButtons(False)
        self.workspace_tabs.setDrawBase(False)
        for key, title in (("roster", "载具识别"), ("events", "战场事件"),
                           ("schemes", "标点方案"), ("alerts", "威胁告警")):
            i = self.workspace_tabs.addTab(tr(title))
            self.workspace_tabs.setTabData(i, key)
            self.workspace_tabs.setTabToolTip(i, tr(title))
        self.workspace_tabs.hide()
        right_layout.addWidget(self.workspace_tabs)
        self.sidebar_stack = QStackedWidget()
        right_layout.addWidget(self.sidebar_stack, 1)
        self.live_sidebar = QWidget()
        self.live_scroll = QScrollArea()
        self.live_scroll.setWidgetResizable(True)
        self.live_scroll.setWidget(self.live_sidebar)
        self.sidebar_stack.addWidget(self.live_scroll)
        self.sidebar_pages = {}
        self.sidebar_scrolls = {}
        self._sidebar_page = "live"
        # 顶对齐堆叠(K-2): 每个面板按最小高度排布 + 底部一个弹性占位 =>
        # 折叠某个面板后, 下面的面板自动往上挪, 不再留一坨被拉伸出来的空白。
        rv = self.right_vbox = QVBoxLayout(self.live_sidebar); rv.setContentsMargins(0, 0, 0, 0)

        # 战场事件流(击杀/被击落/敌方行动), 归属来自 roster —— 默认折成一行, 用户平时不看播报
        self.event_panel = EventPanel()
        ev_box = CollapsibleBox(tr(PANEL_LABELS["events"]))
        ev_box.set_body(self.event_panel)
        self.event_panel.setMinimumHeight(110)   # 最小高度给内容而不是箱子: 折叠时才不留空
        rv.addWidget(ev_box)
        self.panels["events"] = ev_box

        self.alert_panel = AlertPanel(sound_file=self.cfg.alerts.sound_file)
        al_box = CollapsibleBox(tr(PANEL_LABELS["alerts"]))
        al_box.set_body(self.alert_panel)
        rv.addWidget(al_box)
        self.panels["alerts"] = al_box

        # 方案面板
        gb = QWidget()
        gbl = QVBoxLayout(gb)
        actions = QGridLayout()
        b_new = QPushButton(tr("新建")); b_new.clicked.connect(self._new_scheme)
        b_ren = QPushButton(tr("重命名")); b_ren.clicked.connect(self._rename_current)
        b_del = QPushButton(tr("删除")); b_del.clicked.connect(self._delete_current)
        b_bind = QPushButton(tr("绑定任务…")); b_bind.clicked.connect(self._bind_current_task)
        b_new.setProperty("primary", True)
        b_del.setProperty("destructive", True)
        self.scheme_actions = {"new": b_new, "rename": b_ren, "delete": b_del, "bind": b_bind}
        self.export_scheme_button = QPushButton(tr("复制分享码"))
        self.export_scheme_button.setToolTip(tr("分享标点、连线和区域。地图、任务绑定、标签及备注保存在本地。"))
        from PySide6.QtWidgets import QMenu
        export_menu = QMenu(self.export_scheme_button)
        export_menu.addAction(tr("复制汉字短码"), lambda: self._export_scheme(chinese=True))
        export_menu.addAction(tr("复制字母数字码"), self._export_scheme)
        self.export_scheme_button.setMenu(export_menu)
        import_button = QPushButton(tr("导入分享码…"))
        import_button.clicked.connect(self._import_scheme)
        for index, button in enumerate((b_new, b_ren, b_del, b_bind, self.export_scheme_button, import_button)):
            button.setProperty("schemeAction", True)
            actions.addWidget(button, index // 3, index % 3)
        for column in range(3):
            actions.setColumnStretch(column, 1)
        gbl.addLayout(actions)
        self.scheme_task = QPushButton()
        self.scheme_task.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.scheme_task.clicked.connect(self._choose_live_task)
        gbl.addWidget(self.scheme_task)

        # 方案**列表**: 点一下就切换(用户要求, 取代原来的下拉框 —— 下拉框还得先展开再点)
        self.scheme_list = QListWidget()
        self.scheme_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.scheme_list.customContextMenuRequested.connect(self._scheme_menu)
        self.scheme_list.setMaximumHeight(56)      # 3 行够点选, 再多让它内部滚动, 别挤识别列表
        self.scheme_list.setToolTip(tr("点一下切换方案；优先载入当前任务的方案"))
        self.scheme_list.currentItemChanged.connect(self._on_scheme_clicked)
        gbl.addWidget(self.scheme_list)
        self.scheme_empty = QLabel(tr("暂无方案，点击「新建」开始标点"))
        self.scheme_empty.setProperty("muted", True)
        self.scheme_empty.setWordWrap(True)
        gbl.addWidget(self.scheme_empty)

        self.marker_list_label = QLabel(tr("标点与区域"))
        self.marker_list_label.setProperty("muted", True)
        gbl.addWidget(self.marker_list_label)
        self.marker_list = QListWidget()
        self.marker_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.marker_list.setToolTip(tr("单击选中，双击定位"))
        self.marker_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.marker_list.customContextMenuRequested.connect(self._area_list_menu)
        gbl.addWidget(self.marker_list, 1)
        sc_box = CollapsibleBox(tr(PANEL_LABELS["schemes"]))
        sc_box.set_body(gb)
        self.marker_list.setMinimumHeight(60)
        self.marker_list.setMaximumHeight(100)     # 顶部导航也占高度，优先给识别列表留空间
        rv.addWidget(sc_box)
        self.panels["schemes"] = sc_box

        # 载具识别面板(仅本局)
        self.roster_panel = RosterPanel()
        rs_box = CollapsibleBox(tr(PANEL_LABELS["roster"]))
        rs_box.set_body(self.roster_panel)
        self.roster_panel.setMinimumHeight(220)
        # 玩家列表要给足高度(用户: 默认显示不全) => 让它按 9:1 抢剩余空间, 折叠时余量归底部占位
        rv.addWidget(rs_box, 11)
        self.panels["roster"] = rs_box

        rv.addStretch(1)
        split.addWidget(right)
        self.right_widget = right

        # 地图占大头: 参数不占右列空间了(都叠加在地图上), 右列只放事件/方案/识别
        split.setSizes([1180, 380])
        split.splitterMoved.connect(self._remember_workspace_split)
        self.workspace_tabs.currentChanged.connect(self._sync_workspace_panels)
        outer.addWidget(split, 1)
        self.setCentralWidget(central)

        self._refresh_scheme_list()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "right_widget"):
            self._sync_window_layout()

    def set_window_layout(self, mode: str) -> None:
        if mode not in WINDOW_LAYOUTS or self.ui_prefs.window_layout == mode:
            return
        self.ui_prefs.window_layout = mode
        self._save_ui_prefs()
        self._sync_window_layout()

    def _remember_workspace_split(self, _position, _index) -> None:
        first, second = self.split.sizes()
        if first + second:
            if self._portrait:
                self._portrait_map_fraction = first / (first + second)
            else:
                self._landscape_sidebar_fraction = second / (first + second)

    def _sync_window_layout(self) -> None:
        mode = self.ui_prefs.window_layout
        portrait = mode == "portrait" or (mode == "auto" and self.height() > self.width())
        changed = portrait != self._portrait
        if changed:
            self._portrait = portrait
            if portrait:
                self.workspace_layout.insertWidget(0, self.workspace_context)
            else:
                self.sidebar_layout.insertWidget(0, self.workspace_context)
            self.workspace_context.show()
            self.context_hint.setVisible(not portrait)
            self.sidebar_layout.setContentsMargins(*((0, 0, 0, 0) if portrait else (6, 0, 0, 0)))
            self.sidebar_layout.setSpacing(4 if portrait else 8)
            for button in (*self.sidebar_buttons.values(), self.sidebar_back):
                button.setFixedHeight(32 if portrait else 40)
            self.right_widget.setMinimumHeight(145 if portrait else 0)
            self.roster_panel.setMinimumHeight(145 if portrait else 220)
            self.roster_panel.set_portrait(portrait)
            self.map_view.setMaximumHeight(16777215)
            self.split.setOrientation(Qt.Orientation.Vertical if portrait else Qt.Orientation.Horizontal)
            self._sync_workspace_panels()
        if portrait:
            available = self.centralWidget().contentsRect()
            height = (available.height() - self.workspace_context.sizeHint().height()
                      - self.workspace_layout.spacing() - self.split.handleWidth() - 8)
            width = max(100, available.width() - 8)
            self.map_view.setMaximumHeight(width)
            map_height = min(width, round(height * self._portrait_map_fraction), max(120, height - 245))
            self.split.setSizes([map_height, height - map_height])
        elif changed:
            side = round(self.width() * self._landscape_sidebar_fraction)
            self.split.setSizes([self.width() - side, side])

    def _sync_workspace_panels(self, _index=None) -> None:
        tabs = self.workspace_tabs
        tabbed = self._portrait and self._sidebar_page == "live"
        selected = None
        if tabbed:
            with QSignalBlocker(tabs):
                for i in range(tabs.count()):
                    tabs.setTabVisible(i, self.ui_prefs.visible(tabs.tabData(i)))
                index = tabs.currentIndex()
                if index >= 0 and tabs.isTabVisible(index):
                    selected = tabs.tabData(index)
        tabs.setVisible(tabbed and selected is not None)
        for key, box in self.panels.items():
            if box.parentWidget() is self.live_sidebar:
                box.set_tabbed(self._portrait)
                box.setVisible(key == selected if tabbed else self.ui_prefs.visible(key))
                stretch = 1 if self._portrait else (11 if key == "roster" else 0)
                self.right_vbox.setStretchFactor(box, stretch)
            else:
                box.set_tabbed(False)
        self.right_vbox.setStretch(self.right_vbox.count() - 1, int(not self._portrait))

    def _wire(self):
        self.marker_list.itemDoubleClicked.connect(self._on_marker_dbl)
        self.marker_list.itemSelectionChanged.connect(self._on_marker_selected)
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self._undo_shortcut.activated.connect(self._undo_edit)
        for widget in (self.map_view, self.marker_list, self.scheme_list):
            for keys, callback in (("Ctrl+C", self.map_view.copy_annotations),
                                   ("Ctrl+V", self.map_view.paste_annotations),
                                   ("Ctrl+Shift+V", lambda: self.map_view.paste_annotations(in_place=True)),
                                   ("Ctrl+X", self.map_view.move_selected_annotations)):
                shortcut = QShortcut(QKeySequence(keys), widget)
                shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                shortcut.activated.connect(callback)
        for keys in ("Delete", "Backspace"):
            shortcut = QShortcut(QKeySequence(keys), self.marker_list)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.setAutoRepeat(False)
            shortcut.activated.connect(self.map_view.delete_selected_annotations)
        self.map_view._scene.selectionChanged.connect(self._on_map_selection_changed)
        self.map_view.interaction_changed.connect(self._refresh_context)
        self._end_edit_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._end_edit_shortcut.activated.connect(self._end_edit_if_active)
        # 监听 map_view 方案变更以刷新 combo
        self.map_view.active_scheme_changed.connect(self._refresh_scheme_list)
        # 叠加显示开关(姿态仪/仪表)要**记住**: 陆战时用户每次打开都得手动关掉姿态仪
        self.map_view.overlay_items_changed.connect(self._on_overlay_items)
        self.map_view.display_settings_changed.connect(self._refresh_sidebar_settings)
        self.map_view.display_settings_changed.connect(self._refresh_context)
        for key, box in self.panels.items():
            box.toggled.connect(lambda collapsed, k=key: self._on_box_toggled(k, collapsed))

    # ---------------- 威胁告警(附近队友连死 / 同一人连续击杀) ----------------
    def _wire_threat_alerts(self) -> None:
        """地图中的警戒半径用于告警判定。侧栏告警面板负责音效。"""
        # 换图 / 换可视区后 map_view 会按新边长重算半径 → 判定跟着走(看到的圈 = 判定的圈)
        self.map_view.guard_radius_changed.connect(self._nearby.set_radius)
        ratio = self.ui_prefs.guard_radius
        self._on_guard_radius(self.cfg.alerts.nearby_radius if ratio is None else ratio, persist=False)
        self.alert_panel.sound_file_changed.connect(self._sfx.set_path)
        self.alert_panel.test_sound_requested.connect(self._sfx.play)

    def _on_guard_radius(self, ratio: float, persist: bool = True) -> None:
        """面板上的比例 → 圈 = **可视区边长 × 比例**; 判定半径由 map_view 回传(同一个圈)。"""
        self.map_view.set_guard_ratio(ratio)
        if persist:
            self.ui_prefs.set_guard_radius(self.map_view._guard_ratio)
            self._save_ui_prefs()

    def _on_guard_circle(self, on: bool) -> None:
        self.map_view.settings.guard_circle = self.ui_prefs.guard_circle = bool(on)
        self._save_ui_prefs()
        self.map_view.refresh_display()
        self._refresh_sidebar_settings()

    def set_teammate_heading(self, on: bool) -> None:
        self.map_view.settings.teammate_heading = self.ui_prefs.teammate_heading = bool(on)
        self._save_ui_prefs()
        self.map_view.refresh_display()

    def set_map_display_option(self, key: str, on: bool) -> None:
        setattr(self.map_view.settings, key, on)
        if key in ("teammate_trails", "teammate_deaths"):
            setattr(self.ui_prefs, key, on)
            self._save_ui_prefs()
        self.map_view.refresh_display()
        self.map_view.display_settings_changed.emit()

    def set_activity_fill(self, ratio: float) -> None:
        self.map_view.set_activity_fill(ratio)
        self._try_activity_fit()
        self.ui_prefs.activity_fill = self.map_view.activity_fill
        self._save_ui_prefs()

    def set_activity_edge_pan(self, enabled: bool) -> None:
        self.ui_prefs.activity_edge_pan = bool(enabled)
        self.map_view.set_activity_edge_pan(self.ui_prefs.activity_edge_pan)
        self._save_ui_prefs()

    def set_follow_zoom(self, multiplier: float) -> None:
        self.map_view.set_follow_zoom(multiplier)
        self.ui_prefs.follow_zoom = self.map_view.follow_zoom
        self._save_ui_prefs()

    def set_rampage_alerts(self, enabled: bool) -> None:
        self.ui_prefs.rampage_alerts = bool(enabled)
        if not enabled:
            self._rampage.clear_streaks()
        self._save_ui_prefs()

    def set_nearby_death_popup(self, enabled: bool) -> None:
        self.ui_prefs.nearby_death_popup = bool(enabled)
        if not enabled and self._combat_overlay is not None:
            self._combat_overlay.hide_nearby_alert()
        self._save_ui_prefs()

    def set_combat_overlay_rows(self, enabled: bool) -> None:
        self.ui_prefs.combat_overlay_rows = bool(enabled)
        if not enabled and self._combat_overlay is not None:
            self._combat_overlay.clear_rows()
        self._save_ui_prefs()

    def _threat_alert(self, alert: NearbyDeathNotice | RampageNotice) -> None:
        """威胁告警落地: 状态栏文字 + 自定义音效(可空) + 悬浮窗横幅(游戏里看得见)。"""
        msg = alert.message
        self.statusBar().showMessage(f"⚠ {msg}", 8000)
        log.info("威胁告警: %s", msg)
        self._sfx.play()
        mirror = getattr(self, "phone_mirror", None)
        if mirror is not None and mirror.running:
            mirror.bus.push_threat(msg)
        show_popup = self.ui_prefs.nearby_death_popup or isinstance(alert, RampageNotice)
        if self._combat_overlay is not None and show_popup:
            self._combat_overlay.banner(alert, seconds=6.0)

    # ---------------- 面板显隐/折叠(界面偏好, 见 src/agent/uiprefs.py) ----------------
    def _apply_ui_prefs(self) -> None:
        for key, box in self.panels.items():
            box.set_collapsed(self.ui_prefs.collapsed(key), emit=False)
        self._sync_workspace_panels()
        self.roster_panel.set_alive_only(self.ui_prefs.alive_only)
        self.roster_panel.set_highlight_killers(self.ui_prefs.highlight_killers)
        self.map_view.settings.guard_circle = self.ui_prefs.guard_circle
        self.map_view.settings.teammate_trails = self.ui_prefs.teammate_trails
        self.map_view.settings.teammate_deaths = self.ui_prefs.teammate_deaths
        self.map_view.settings.teammate_heading = self.ui_prefs.teammate_heading
        self.map_view.set_activity_fill(self.ui_prefs.activity_fill)
        self.map_view.set_activity_edge_pan(self.ui_prefs.activity_edge_pan)
        self.map_view.set_follow_zoom(self.ui_prefs.follow_zoom)
        self.map_view.overlay.set_aircraft_tilt(self.ui_prefs.attitude_aircraft_tilt)
        self._apply_overlay_items()                      # 恢复叠加显示开关(并按载体类型取舍)
        self._sync_right_visibility()
        self._update_badges()

    def _on_alive_only(self, value: bool) -> None:
        self.ui_prefs.set_alive_only(bool(value))
        self._save_ui_prefs()

    def _on_highlight_killers(self, value: bool) -> None:
        self.ui_prefs.highlight_killers = bool(value)
        self._save_ui_prefs()

    def _on_battle_state(self, in_battle: bool) -> None:
        """`map_info.valid` 翻转 -> 离开战场(回大厅)或进入新的一局, 清识别池。

        这条专门补 time 判据的漏洞: 提前退出对局时旧局 time 可能只走到 90，
        下一局从 100 起是**递增**，只看 time 回落就漏掉了边界（用户实测到的现象）。
        """
        if self.battle.map_info_valid(in_battle):
            self._awaiting_battle_map = True
            self._keep_active_plan()
            self._on_new_battle(self.battle.last_reason)
        if in_battle and self._awaiting_battle_map:
            self._awaiting_battle_map = False
            self._reset_game_task()
        self._ticket_battle_active = in_battle
        if not in_battle:
            self.map_view.set_ticket_eta(None)

    def _on_box_toggled(self, key: str, collapsed: bool) -> None:
        self.ui_prefs.set(key, collapsed=collapsed)
        self._save_ui_prefs()
        self._update_badges()

    def _set_panel_collapsed(self, key: str, collapsed: bool) -> None:
        box = self.panels.get(key)
        if box is None or box.is_collapsed() == bool(collapsed):
            return
        box.set_collapsed(collapsed)              # 经 toggled 信号落盘

    def _set_panel_visible(self, key: str, visible: bool) -> None:
        box = self.panels.get(key)
        if box is None:
            return
        self.ui_prefs.set(key, visible=visible)
        self._sync_workspace_panels()
        self._sync_right_visibility()
        self._save_ui_prefs()
        self._refresh_sidebar_settings()

    def _sync_right_visibility(self) -> None:
        """顶部入口始终可用，面板全关后仍可进入设置恢复。"""
        self.right_widget.setVisible(True)

    def _toggle_sidebar(self, key: str) -> None:
        if key == "library":
            if self._library_mode:
                self.follow_game_map()
            else:
                self.open_map_library()
            return
        page = ("library" if self._library_mode else "live") if self._sidebar_page == key else key
        self._show_sidebar(page)

    def _back_to_workspace(self) -> None:
        if self._sidebar_page == "library":
            self.follow_game_map()
        else:
            self._show_sidebar("library" if self._library_mode else "live")

    def _end_edit_if_active(self):
        if self.map_view._walk_mode or self.map_view._connect_from or self.map_view._area_editor:
            self.map_view.end_editing()

    def _refresh_context(self) -> None:
        if not hasattr(self, "sidebar_back"):
            return
        mv = self.map_view
        self.map_title.set_title(map_name(self._scene) if self._scene else tr("等待地图"))
        if self._map_locked:
            state, tone = tr("离线规划"), "planning"
        elif not getattr(self, "_online", False):
            state, tone = tr("未连接游戏"), "waiting"
        elif mv._objs_stale or not mv._last_objs:
            state, tone = tr("等待实时数据"), "waiting"
        else:
            state, tone = tr("实时战况"), "live"
        self.context_badge.setText(state)
        if self.context_badge.property("tone") != tone:
            self.context_badge.setProperty("tone", tone)
            self.context_badge.style().unpolish(self.context_badge)
            self.context_badge.style().polish(self.context_badge)
        if mv._area_editor:
            hint = tr('绘制{0}', tr(AREA_TYPES[mv._area_editor.kind]))
        elif mv._connect_from:
            hint = tr("连线中")
        elif mv._walk_mode:
            hint = tr("连续标点")
        elif self._map_locked:
            hint = tr("离线编辑")
        elif mv.settings.follow_player:
            hint = tr("跟随玩家")
        elif mv.activity_fit_enabled():
            hint = tr("自动放大地图")
        else:
            hint = tr("手动视角")
        self.context_hint.setText(hint)
        page = getattr(self, "_sidebar_page", "live")
        self.sidebar_back.set_label(tr("返回游戏") if page == "library" else
                                  tr("返回规划") if self._library_mode else
                                  tr("当前战况") if page == "live" else tr("返回战况"))
        self.sidebar_back.setEnabled(page != "live" or self._library_mode)
        mirror = getattr(self, "phone_mirror", None)
        self.sidebar_buttons["phone"].set_indicator(bool(mirror and mirror.running))
        self.sidebar_buttons["phone"].set_label(tr("手机显示 · 已开启") if mirror and mirror.running else tr("手机显示"))

    def _show_sidebar(self, key: str) -> None:
        if key == "live":
            page = self.live_scroll
        else:
            created = key not in self.sidebar_pages
            if created:
                if key == "library":
                    from .map_library import MapLibrarySidebar
                    page = MapLibrarySidebar(self)
                    page.editor.map_chosen.connect(self.choose_map)
                elif key == "phone":
                    from .phone_panel import PhonePanel
                    page = PhonePanel(self)
                else:
                    from .sidebar_settings import SidebarSettings
                    page = SidebarSettings(self, key)
                self.sidebar_pages[key] = page
                if key in ("library", "phone"):
                    scroll = QScrollArea()
                    scroll.setWidgetResizable(True)
                    scroll.setWidget(page)
                    self.sidebar_scrolls[key] = scroll
                    self.sidebar_stack.addWidget(scroll)
                else:
                    self.sidebar_stack.addWidget(page)
            page = self.sidebar_pages[key]
            if key != "library" or not created:
                page.refresh()
        scheme_box = self.panels["schemes"]
        if key == "library":
            page.layout().insertWidget(0, scheme_box)
            scheme_box.setVisible(True)
        elif key == "live":
            self.right_vbox.insertWidget(2, scheme_box)
        self.sidebar_stack.setCurrentWidget(self.sidebar_scrolls.get(key, page))
        self._sidebar_page = key
        self._sync_workspace_panels()
        for name, button in self.sidebar_buttons.items():
            button.setChecked(name == key)
        self._refresh_context()

    def _refresh_sidebar_settings(self) -> None:
        for key, page in self.sidebar_pages.items():
            if key != "library":
                page.refresh()

    def _save_ui_prefs(self) -> None:
        self.ui_prefs.save(self.data_root)        # 写不进去也不影响使用(内部静默)

    def set_interface_language(self, target: str) -> bool:
        target = normalize_language(target)
        previous = self.ui_prefs.language
        self.ui_prefs.language = target
        if self.ui_prefs.save(self.data_root) is None:
            self.ui_prefs.language = previous
            self.statusBar().showMessage(tr("无法保存界面语言。"), 6000)
            return False
        if target != language():
            self.statusBar().showMessage(tr("语言已保存，重新打开终端后生效。"), 10000)
        return True

    def _on_overlay_items(self, *_args) -> None:
        """勾选的是飞机读数偏好，隐藏整组时也不丢失用户选择。"""
        items = sorted(self.map_view.overlay_items())
        self.ui_prefs.set_overlay_items(items)
        self._save_ui_prefs()
        self._refresh_sidebar_settings()

    def set_attitude_aircraft_tilt(self, enabled: bool) -> None:
        self.ui_prefs.attitude_aircraft_tilt = bool(enabled)
        self.map_view.overlay.set_aircraft_tilt(enabled)
        self._save_ui_prefs()
        self._refresh_sidebar_settings()

    def set_ticket_eta_visible(self, enabled: bool) -> None:
        self.ui_prefs.show_ticket_eta = bool(enabled)
        self._save_ui_prefs()
        self._refresh_ticket_eta()

    # ---------------- 载具读数只对已确认的飞机显示 ----------------
    def _set_vehicle_kind(self, kind: str) -> None:
        """仅 Player 的 aircraft 类型可开启仪表，与地图分类和战斗模式无关。"""
        if kind == self._veh_kind:
            return
        self._veh_kind = kind
        # 空帧/换图期间保留飞机限制，直到本人载具重新确认，避免视角突然放大。
        if kind != "unknown":
            self.map_view.set_aircraft_active(kind == "air")
        self._apply_overlay_items()

    def _update_vehicle_kind(self, objs) -> None:
        me = next((o for o in objs if o.is_player), None)
        kind = str(getattr(me, "type", "") or "").lower()
        self._set_vehicle_kind("air" if kind == "aircraft" else
                               "ground" if kind.startswith("ground") else
                               "other" if kind and kind != "unknown" else "unknown")

    def _apply_overlay_items(self) -> None:
        """只恢复飞机偏好；旧地面偏好/auto_by_vehicle 不再绕过飞机限制。"""
        air = self.ui_prefs.overlay_items
        if air is None:
            air = [str(x) for x in (self.cfg.attitude.show or ("attitude",))]
        self.map_view.set_overlay_items(air)
        self.map_view.overlay.set_flight_active(self._veh_kind == "air" and not self._map_locked)
        self._refresh_sidebar_settings()

    def panel_menu_entries(self) -> list[tuple[str, str, bool]]:
        """侧边栏面板设置项；旧 maps 偏好保留兼容，地图库使用独立入口。"""
        return [(k, tr(PANEL_LABELS[k]), self.ui_prefs.visible(k)) for k in self.panels]

    def _feed_line_count(self) -> int:
        return max(0, self.event_panel.blockCount() - 1)

    def _update_badges(self) -> None:
        """折起来也看得见"刚刚发生了多少事" —— 只挂计数, 不展开面板。"""
        self.panels["events"].set_badge(tr('{0} 条', self._feed_line_count()))
        names = self.roster.display_names()
        if self.roster_panel.alive_only():          # 以面板实际状态为准(而不是偏好副本)
            # 勾了"隐藏已离场"时, 徽章要把"看到几个/总共几个"一起说清, 免得以为列表坏了
            left = sum(1 for n in names if self.roster.status_of(n) == "left")
            self.panels["roster"].set_badge(tr('{0}/{1} 人 · 隐藏离场', len(names) - left, len(names)))
        else:
            self.panels["roster"].set_badge(tr('{0} 人', len(names)))

    # ---------------- Poller slots ----------------
    def _on_map(self, img, scene):
        scene = mapimg.resolve_scene(self.data_root, scene)
        self._game_image = (img, scene)
        if scene is not None and (scene != self._game_scene or (not self._map_locked and scene != self._scene)):
            self._on_scene(scene)
        if self._map_locked:
            return                                # 手动选图(离线规划)中: 别让游戏的图顶掉
        self.map_view.set_scene_name(self._scene)
        self.map_view.load_image(img, new_scene=self._display_image_scene != self._scene)
        self._display_image_scene = self._scene
        self._try_activity_fit()              # 对象可能比底图先到，不能只在对象回调里尝试
        self._refresh_context()

    def _try_activity_fit(self) -> None:
        if self._map_locked or self._scene is None or self._display_image_scene != self._scene:
            return
        # 每个实时帧都可检查：仅出生点/占领区范围改变才实际调整，空帧和缓存不消耗等待。
        if self.map_view.fit_activity_area():
            self._fit_pending = False

    def _on_map_info(self, mi: MapInfo):
        self._map_info = mi
        if mi.valid:
            mm = mi.map_min; mM = mi.map_max
            gs = mi.grid_steps
            self.statusBar().showMessage(
                tr('战场 {0:.0f}x{1:.0f}km 网格{2:.0f}km', (mM[0] - mm[0]) * 100, (mM[1] - mm[1]) * 100, gs[0] * 100), 5000)

    def _on_indicators(self, att) -> None:
        """姿态交给叠加层；顺便把 `vehicle`(=/indicators 的 type) 喂给载具识别当"我开的是什么"。

        文案里通常根本没有我自己的载具（除非我参与击杀/被击落），所以本人载具得靠遥测补。
        """
        self.map_view.set_attitude(att)
        if self.roster.set_self_vehicle(getattr(att, "vehicle", None)):
            self._roster_dirty = True

    def _remember_map_kind(self, objs) -> None:
        """依据当前地图的可见跑道更新自动分类，手动分类始终优先。"""
        from ..agent import mapimg
        if not self._scene:
            return
        if not mapimg.map_cache_path(self.data_root, self._scene).is_file():
            return  # 底图尚未成功下载时，禁止用类型元数据造空目录。
        kind = mapimg.guess_kind(objs)
        if kind == "unknown":
            return
        key = (self._scene, kind)
        if getattr(self, "_scene_kind", None) == key:
            return                       # 同一局同一张图只判一次, 不做每帧读盘
        mapimg.remember_auto_kind(self.data_root, self._scene, kind)
        self._scene_kind = key

    def _render_game_objects(self, objs) -> None:
        """把一帧游戏地图对象画到地图上（含"这一帧没有数据"的三种情形）。

        用户 2026-10-23 报「怎么不显示玩家自己了、还有占领区、出生点」的真机根因：
        游戏在**部署界面 / 阵亡等待界面**对 `/map_obj.json` 返回 **200 + 0 字节**，
        `poller` 里 `r.json()` 抛异常被 `except Exception: pass` 吞掉 ⇒ 这一帧根本没有对象。
        此时:
          1. 有上一帧 ⇒ 保留(淡显), 不清空 —— 至少还看得见自己最后位置和占领区;
          2. 没有上一帧(刚启动/刚换图) ⇒ 用**缓存**的占领区+出生点(淡显), 不让地图空着。
        """
        objs = list(objs or ())
        if self._map_locked:
            return
        if objs:
            self._cache_display_scene = None
            self.map_view.set_map_objects(objs)
            self.map_view.resolve_teammate_deaths(self.roster.side_of, self._clock())
            self._remember_map_kind(objs)
            self._remember_statics(objs)
            self._last_live_at = self._clock()
            self._try_activity_fit()
            return
        # 没有实时帧: 上一帧先淡显保留一会儿(阵亡/部署界面常常几秒钟), 但**不能一直留着** ——
        # 几分钟前的敌人图标当实时情报用是误导。超过 STALE_HOLD_SEC 就只留缓存的静态要素。
        fresh_enough = self.map_view.last_objects() and (
            self._clock() - self._last_live_at <= STALE_HOLD_SEC)
        if fresh_enough:
            self.map_view.set_map_objects([])        # 内部: 保留上一帧 + 淡显
            return
        scene = self._scene or self._game_scene
        if scene is not None and self._cache_display_scene == scene:
            return                              # 缓存已显示，不在每个空帧重复读盘/重建
        if self.map_view.last_objects():
            self.map_view.clear_objects()            # 过期的单位/自己被清掉
        cached = mapstatics.load(self.data_root, self._scene or self._game_scene)
        self._cache_display_scene = scene
        if cached:
            self.map_view.set_map_objects(cached, record_tracks=False, stale=True)
            self.statusBar().showMessage(tr("暂无实时地图数据：显示缓存的占领区 / 出生点"), 3000)
        else:
            # 连缓存都没有(新图 + 还没在对局里见过实时帧): 至少在视口里说清楚在等什么
            self.map_view.set_map_objects([], record_tracks=False)

    def _expire_game_objects(self) -> None:
        self._refresh_ticket_eta()
        if self._clock() - self._game_objects_at > LIVE_FRAME_SEC:
            self._set_vehicle_kind("unknown")
        if not self._map_locked and self._clock() - self._last_live_at > LIVE_FRAME_SEC:
            self._render_game_objects([])

    def _remember_statics(self, objs) -> None:
        """把占领区/出生点/我的位置缓存下来（只有变化时才写盘）。"""
        scene = self._scene or self._game_scene
        if not scene:
            return
        items = mapstatics.extract(objs)
        fp = mapstatics.fingerprint(items)
        if not fp or self._statics_fp.get(scene) == fp:
            return
        try:
            if mapstatics.remember(self.data_root, scene, objs):
                self._statics_fp[scene] = fp
            else:
                self._statics_fp[scene] = fp          # 内容没变(或没有静态要素): 记下指纹别再算
        except OSError:                                # 落盘失败不该影响实时显示
            log.warning("缓存地图静态要素失败: %s", scene, exc_info=True)

    def _on_map_objects(self, objs):
        objs = list(objs or ())
        self._game_objects = objs
        self._game_objects_scene = self._game_scene
        self._game_objects_at = self._clock() if objs else -1e9
        ground_hud = self._map_info is not None and self._map_info.hud_type == 1
        if (self._ticket_battle_active and self._game_scene is not None
                and (ground_hud or any(obj.type == "respawn_base_tank" for obj in objs))
                and self._tickets.observe(objs, self._game_objects_at)):
            self._ticket_scene = self._game_scene
            self._refresh_ticket_eta()
        if objs and self._game_scene and self._game_task is None and not self._awaiting_battle_map:
            candidate = task_layout.recognize(objs, self._live_task_catalog())
            if candidate is not None and candidate == self._task_candidate:
                self._game_task = candidate
                self._apply_live_task()
            self._task_candidate = candidate
        self._update_vehicle_kind(objs)           # 必须先判本人载具，再尝试自动放大
        self._render_game_objects(objs)
        # 地图上的友/敌单位数: 用来解释"文案识别到的人数"为什么和局内人数不一致
        friend = sum(1 for o in objs if getattr(o, "side", "") == "friend" and o.x is not None)
        enemy = sum(1 for o in objs if getattr(o, "side", "") == "enemy" and o.x is not None)
        self.roster_panel.set_map_counts(friend, enemy)
        # 击杀↔图标消失关联的帧 feed: 只数"单位"(口径与 map_view 一致:
        # 排除线段/占领区/出生点/我自己), 友军计数是它的主证据
        def _is_unit(o) -> bool:
            return (o.x is not None and not o.is_segment and not o.is_player
                    and o.type != "capture_zone" and o.icon != "capture_zone"
                    and not o.type.startswith("respawn_base"))
        self._killcorr.feed_frame(
            sum(1 for o in objs if _is_unit(o) and o.side == "friend"),
            sum(1 for o in objs if _is_unit(o) and o.side == "enemy"),
            now=self._clock())
        # 威胁告警①: 附近队友连续阵亡(友方单位图标在我警戒半径内消失)
        # frame_alive: 帧里还有别的对象(比如我自己)时, 友方列表空 = 真没了, 不是打嗝
        me_xy = next(((o.x, o.y) for o in objs if o.is_player and o.x is not None), None)
        notice = self._nearby.feed(
            [(o.x, o.y, o.type) for o in objs if _is_unit(o) and o.side == "friend"],
            me_xy, now=self._clock(), frame_alive=bool(objs))
        if notice:
            self._threat_alert(notice)
        if not self.map_view._area_editor:
            self.statusBar().showMessage(tr('对象: {0}', len(objs)), 2000)

    def _refresh_ticket_eta(self) -> None:
        seconds = None
        if (self.ui_prefs.show_ticket_eta and self._ticket_battle_active and not self._map_locked
                and not self._ticket_mission_finished
                and self._ticket_scene is not None and self._ticket_scene == self._game_scene):
            seconds = self._tickets.seconds_left(self._clock())
        self.map_view.set_ticket_eta(seconds)

    def _on_mission_status(self, status: str) -> None:
        if status in ("success", "fail", "failed"):
            self._ticket_mission_finished = True
            self.map_view.set_ticket_eta(None)
        elif status == "running":
            self._ticket_mission_finished = False
            self._refresh_ticket_eta()

    def _on_hud_events(self, evs):
        # 先吸收进 roster 建边, 再分类: 这样同一批里的击杀也能立刻参与阵营染色
        # 边界常落在**批内**(首包混着上一局的尾巴), 所以只吸收切点之后的事件。
        evs = list(evs or ())
        cleared = False
        # K-1: 嘉奖类边界优先 —— 「先拔头筹」=开局、「最后一击/最终赢家/完成了一击」=收尾。
        # 陆战可以反复复活, 所以"我被击毁"不能当边界用。
        for e in evs:
            ev = parse_line(getattr(e, "msg", "") or "")
            if ev is not None and ev.kind in ("first_blood", "last_kill"):
                if self.battle.on_award(ev.kind, float(getattr(e, "time", 0.0) or 0.0)):
                    self._on_new_battle(self.battle.last_reason)
                    cleared = True
        cut, new_battle = self.battle.scan(getattr(e, "time", None) for e in evs)
        if new_battle and not cleared:
            self._on_new_battle(self.battle.last_reason)
        if cut:
            evs = evs[cut:]
        if not evs:
            return
        if self.roster.absorb_events(evs):
            self._roster_dirty = True
        # 击杀 ↔ 地图图标消失关联(敌我补强): 先记时间锚点, 击杀进队列延迟 ~1.2s 判定
        # (文案到达时击杀后的地图帧还没攒够); 友军少了→受害者是友军, 没少→敌军(用户规则)。
        now = self._clock()
        latest_time = max((float(e.time) for e in evs
                           if getattr(e, "time", None) and isfinite(float(e.time))), default=0.0)
        if latest_time:
            self._killcorr.anchor_hud(latest_time, now)
        track_deaths = self.map_view.settings.teammate_deaths and not self._map_locked
        streaks_enabled = self.ui_prefs.rampage_alerts
        streak_inputs: list[tuple[float, CombatEvent]] = []
        for e in evs:
            ev = parse_line(getattr(e, "msg", "") or "")
            if ev is None:
                continue
            timestamp = float(getattr(e, "time", 0.0) or 0.0)
            if track_deaths and (name := ev.player_death_name):
                self.map_view.queue_teammate_death(name, timestamp, now - (latest_time - timestamp))
            if ev.kind == "award" and ev.award_streak and streaks_enabled:
                streak_inputs.append((timestamp, ev))
            if self._ticket_battle_active:
                if ev.kind == "kill" and ev.is_pair and ev.victim_is_player:
                    self._tickets.record_death(ev.victim, timestamp)
                elif ev.kind == "crashed":
                    self._tickets.record_death(ev.subject, timestamp)
            if ev.kind != "kill" or not ev.is_pair:
                continue
            dk = (normalize_key(ev.actor), normalize_key(ev.victim), timestamp)
            if dk in self._corr_seen:
                continue                        # 真机同一次击杀会发两条文案
            self._corr_seen.add(dk)
            if ev.is_player_pair:
                self._pending_kills.append((ev, timestamp, 0))
            if streaks_enabled:
                streak_inputs.append((timestamp, ev))
        if track_deaths:
            self.map_view.resolve_teammate_deaths(self.roster.side_of, now)
        self._tickets.resolve_deaths(self.roster.side_of)
        self._refresh_ticket_eta()
        # 按局内时间合并两个来源。同秒先吃击杀，再消化对应嘉奖。
        for timestamp, ev in sorted(streak_inputs, key=lambda item: (item[0], item[1].kind == "award")):
            if ev.kind == "award":
                notice = self._rampage.feed_award(ev.subject, ev.subject_vehicle, ev.award_name,
                                                  ev.award_streak, timestamp)
            else:
                notice = self._rampage.feed_kill(ev.actor, ev.actor_vehicle, ev.victim, timestamp)
            if notice:
                self._threat_alert(notice)
        if self._pending_kills:
            QTimer.singleShot(1200, self._flush_kill_judgments)
        items, rings = build_feed(evs, self.roster, self.map_view.settings, seen=self._feed_seen)
        if len(self._feed_seen) > 1000:
            self._feed_seen = set(sorted(self._feed_seen)[-500:])
        if items:
            self.event_panel.add_items(items)
        # 交战车型速览(悬浮窗): 我打人/人打我且**未死亡**的伤害文案(重创/引燃/击伤…), 带对方车型
        if self._combat_overlay is not None and self.ui_prefs.combat_overlay_rows:
            for it in items:
                if it.kind == "damage" and it.category in ("self_action", "self_victim"):
                    self._combat_overlay.add_damage(it)
        by_id = {it.id: it for it in items}
        for e in (() if self._map_locked else rings):
            it = by_id.get(getattr(e, "id", 0))
            self.map_view.mark_my_death(it.display if it else tr("我被击落"))
        self._announce(self.alerts.on_feed(items))
        self._update_badges()                   # 面板折着也要能看见"又来了几条"

    def _flush_kill_judgments(self) -> None:
        """延迟判定队列: 等击杀后的地图帧到位再做关联, 判出的阵营钉进 roster(图染色优先)。

        证据不足(None)时最多重试 2 次(地图帧可能滞后); 试完仍不足就放弃 —— 宁可不钉也不错钉。
        """
        if not self._pending_kills:
            return
        now = self._clock()
        pending, self._pending_kills = self._pending_kills, []
        retry = False
        for ev, t, attempts in pending:
            side = self._killcorr.judge_kill(t, now=now)
            if side == "friend":                            # 友军图标 −1: 受害者是友军
                self.roster.pin_camp(ev.victim, 1)
                self.roster.pin_camp(ev.actor, -1)
                self._roster_dirty = True
            elif side == "enemy":                           # 友军没少: 受害者是敌军
                self.roster.pin_camp(ev.victim, -1)
                self.roster.pin_camp(ev.actor, 1)
                self._roster_dirty = True
            elif attempts < 2:
                self._pending_kills.append((ev, t, attempts + 1))
                retry = True
        if not self._map_locked:
            self.map_view.resolve_teammate_deaths(self.roster.side_of, now)
        self._tickets.resolve_deaths(self.roster.side_of)
        self._refresh_ticket_eta()
        if retry:
            QTimer.singleShot(1200, self._flush_kill_judgments)

    def _on_state(self, st):
        if not st or not st.get("valid"):
            self._set_vehicle_kind("unknown")
        self.map_view.set_telemetry(parse_state(st))
        self._announce(self.alerts.on_state(st))

    def _announce(self, reasons) -> None:
        """告警落地: 只显示在状态栏（提示音已按用户要求彻底移除，不留开关）。"""
        for msg in reasons or ():
            self.statusBar().showMessage(f"⚠ {msg}", 6000)
            log.info("告警: %s", msg)

    # ---------------- 载具识别(仅本局) ----------------
    def _tick_roster(self):
        if self._roster_dirty:
            self._roster_dirty = False
            self.roster_panel.set_roster(self.roster, self.cfg.identity.my_nick)
            self.nickname_label.set_title(tr('我：{0}', self.roster.self_display_name()) if self.cfg.identity.my_nick else tr("未设置游戏昵称"))
            self._update_badges()

    def _fresh_roster(self) -> None:
        if self._combat_overlay is not None:
            self._combat_overlay.clear()
        mirror = getattr(self, "phone_mirror", None)
        if mirror is not None and mirror.running:
            mirror.bus.reset_battle()
        self.roster = Roster(my_nick=self.cfg.identity.my_nick)
        # 击杀↔图标关联也随之一并清空(本局内存, 不跨局)
        self._killcorr.reset()
        self._pending_kills.clear()
        self._corr_seen.clear()
        # 威胁告警同理: 死亡帧史/连杀史/冷却全部归零(本局内存, 不跨局)
        self._nearby.reset()
        self._rampage.reset()
        self._roster_dirty = False
        self.roster_panel.set_roster(self.roster, self.cfg.identity.my_nick)
        self.nickname_label.set_title(tr('我：{0}', self.roster.self_display_name()) if self.cfg.identity.my_nick else tr("未设置游戏昵称"))
        self._update_badges()

    def _export_roster(self):
        """只导出人读的 txt(识别结果不再落盘累积)。"""
        try:
            self.roster_dir.mkdir(parents=True, exist_ok=True)
            out = self.roster_dir / "roster_latest.txt"
            out.write_text(self.roster.to_text(), encoding="utf-8")
            self.statusBar().showMessage(tr('已导出 {0}', out), 8000)
            QMessageBox.information(self, tr("导出本局载具识别"), str(out))
        except Exception:
            log.exception(tr("导出失败"))

    def _clear_roster(self):
        if QMessageBox.question(self, tr("确认"), tr("清空本局的载具识别？（不影响标点方案）")) != QMessageBox.Yes:
            return
        self._fresh_roster()

    def _on_connection(self, online: bool) -> None:
        self._online = bool(online)
        self.map_view.set_online(self._online)
        if self.battle.connection(online):
            # 重连=客户端可能重启, time/id 空间都会重来 => 按边界处理(需持续离线超过去抖窗口)
            self._on_new_battle(self.battle.last_reason)
        if not self._online:
            self._ticket_battle_active = False
            self.map_view.set_ticket_eta(None)
            self._awaiting_battle_map = True
            self._set_vehicle_kind("unknown")
            self._render_game_objects([])
            self.statusBar().showMessage(
                tr("未连接到战雷战术接口(localhost:8111)。请在游戏设置中开启 Live 战术接口。"), 0)

    def _on_scene(self, scene):
        scene = mapimg.resolve_scene(self.data_root, scene)
        if scene != self._game_scene:
            self._game_task = self._task_candidate = None
            self.map_view.set_ticket_eta(None)
        self._game_scene = scene
        if self._map_locked:
            return                                # 手动选图(离线规划)中: 不跟随游戏换图
        if scene == self._scene:
            return
        if self._keep_active_plan():
            return
        self._set_vehicle_kind("unknown")
        self._scene = scene
        self._display_image_scene = None
        self._cache_display_scene = None
        self.map_view.set_scene_name(scene)
        # 换图: 清空轨迹与事件标记(视为全新对局/重连)
        self.map_view.on_reconnect()
        # 换图后清除旧活动区，等新地图对象到达后按当前模式定位。
        self.map_view.reset_fit_lock()
        self._fit_pending = True
        # 换图后若是**部署/阵亡界面**(/map_obj 返回 0 字节), 也要先把这张图缓存的
        # 占领区与出生点画出来, 否则刚启动时整张图只有一个底图(用户 2026-10-23 报的现象)
        self._render_game_objects([])
        # 加载该场景方案库
        self._schemes = markers_store.load_schemes(self.data_root, scene)
        self._set_task_scope(self._game_task)
        # 注意: 换图/换视角**不再清空识别池**。真机取证: 陆战出击载具在坦克↔飞机之间切换会换
        # 两张不同几何的图, 那是同一局 —— 以前把它当边界就是"名单莫名其妙被清空"的真凶。
        self.battle.scene_changed(scene)

    # ---------------- 地图库: 离线选图 / 规划 (用户 2026-10-12) ----------------
    @mapimg.synchronized
    def maintain_map_library(self) -> dict:
        self._startup_library_timer.stop()
        empty = {"merged": [], "removed": []}
        if self._library_future is not None:
            return {**empty, "pending": True}
        snapshot = mapimg.library_snapshot(self.data_root)
        if snapshot == self._library_clean:
            return empty
        # 很小的缓存可立即完成。正常底图的解码和全库比对由单个后台任务负责。
        if len(snapshot) <= 4 and sum(size for _, size, _ in snapshot) <= 65536:
            try:
                report = self._apply_library_scan(snapshot, mapimg.matching_pairs(snapshot))
                if report is not None:
                    return report
            except OSError:
                pass  # 文件在读取中改动或暂时不可读，交给后台检查并报告。
        self._library_scan_snapshot = snapshot
        self._library_future = self._library_executor.submit(mapimg.matching_pairs, snapshot, self._library_cancel)
        self._library_timer.start()
        return {**empty, "pending": True}

    def _finish_library_scan(self):
        future = self._library_future
        if future is None or not future.done():
            return
        self._library_timer.stop()
        self._library_future = None
        snapshot = self._library_scan_snapshot
        try:
            neighbors = future.result()
            report = self._apply_library_scan(snapshot, neighbors)
        except OSError:
            if snapshot == mapimg.library_snapshot(self.data_root):
                log.exception(tr("读取地图库失败，已保留来源，下次刷新重试"))
                self._library_notify = False
                self.statusBar().showMessage(tr("部分底图读取失败，可刷新地图库重试"), 5000)
                return
            report = None
        if report is None:
            report = self.maintain_map_library()
            if report.get("pending"):
                return
        if report["merged"] or report["removed"]:
            if "library" in self.sidebar_pages:
                self.sidebar_pages["library"].editor._reload(report)
        if self._library_notify:
            self._library_notify = False
            merged = sum(len(names) for _, names in report["merged"])
            self.statusBar().showMessage(tr('地图库整理完成 · 合并 {0} 张 · 清理 {1} 张', merged, len(report['removed'])), 5000)

    def _apply_library_scan(self, snapshot, neighbors):
        report = mapimg.apply_library_scan(self.data_root, snapshot, neighbors)
        if report is None:
            return None
        if not neighbors and all(size for _, size, _ in snapshot):
            self._library_clean = snapshot
        else:
            self._library_clean = None
        mapping = {source: keep for keep, sources in report["merged"] for source in sources}
        if not mapping:
            return report
        self._rebind_map_scenes(mapping)
        return report

    def _rebind_map_scenes(self, mapping: dict[str, str]) -> None:
        if self._game_scene in mapping or self._game_scene in mapping.values():
            self._task_catalog_scene = None
        self._game_scene = mapping.get(self._game_scene, self._game_scene)
        self._game_objects_scene = mapping.get(self._game_objects_scene, self._game_objects_scene)
        self._display_image_scene = mapping.get(self._display_image_scene, self._display_image_scene)
        if self._game_image is not None:
            image, scene = self._game_image
            self._game_image = (image, mapping.get(scene, scene))
        for scene in {*mapping, *mapping.values()}:
            self._statics_fp.pop(scene, None)
        self._cache_display_scene = None
        if self._scene in mapping or self._scene in mapping.values():
            previous_id = self._current.id if self._current else None
            self._scene = mapping.get(self._scene, self._scene)
            self.map_view.set_scene_name(self._scene)
            self._schemes = markers_store.load_schemes(self.data_root, self._scene)
            self._current = next((scheme for scheme in self._visible_schemes() if scheme.id == previous_id),
                                 self._default_scheme())
            self.map_view.set_scheme(self._current)
            self._refresh_scheme_list()

    def open_map_library(self) -> None:
        """在右侧栏进入地图库编辑状态，不再弹独立窗口。"""
        self._map_locked = True
        self._set_planning_view(True)
        self.sidebar_pages["library"].editor.select_displayed_task()
        self._refresh_scheme_list()
        self.statusBar().showMessage(tr("地图库"), 3000)

    def _keep_active_plan(self) -> bool:
        if self._map_locked or not self.map_view.has_active_plan_edit():
            return False
        self.open_map_library()
        self.map_view.on_reconnect()
        return True

    def choose_map(self, scene: str, layout_index: int = 0) -> None:
        """手动选一张图来规划: 用缓存底图(没有就给网格画布), 载入它的方案, 并**锁住**不跟随游戏。"""
        from PySide6.QtGui import QImage
        from ..agent import mapimg
        from .map_library import offline_canvas

        self._map_locked = True
        self._scene = scene
        self.map_view.set_scene_name(scene)
        self.map_view.on_reconnect()               # 清掉上一张图的轨迹/事件标记
        self.map_view.reset_fit_lock()

        base = mapimg.map_cache_path(self.data_root, scene)
        img = QImage(str(base)) if base.is_file() else QImage()
        note = ""
        if img.isNull():
            img = offline_canvas(scene)            # 没底图也能先摆标点
            note = tr(" · 无底图")
        self.map_view.load_image(img)
        self.map_view.fit_whole_map()              # 离线编辑应从整图开始，不继承上一张任务的缩放

        self._schemes = markers_store.load_schemes(self.data_root, scene)
        self.map_view.clear_objects()      # 离线规划图不显示游戏实时对象(与"空帧"区分, 见 clear_objects)
        self._set_planning_view(True)
        self.statusBar().showMessage(
            tr('离线规划：{0}{1}', map_name(scene), note), 3000)
        self.show_public_layout(layout_index)
        self.sidebar_pages["library"].editor.select_displayed_task()

    def show_public_layout(self, index: int) -> None:
        """公开简报布局仅用于离线规划，阵营按任务中的两方显示。"""
        if not self._map_locked:
            return
        layouts = mapimg.read_meta(self.data_root, self._scene).get("public_layouts", [])
        if not 0 <= index < len(layouts):
            self.map_view.clear_objects()
            self._set_task_scope(None)
            return
        from ..agent.mapobj import parse_map_objects
        layout = layouts[index]
        self._set_task_scope(task_layout.prepare(layouts)[index])
        self.map_view.clear_objects()
        objects = parse_map_objects(layout.get("objects", []))
        if objects:
            self.map_view.set_map_objects(objects, record_tracks=False, public_layout=True)
        self.statusBar().showMessage(tr('离线规划：{0} · {1} · 蓝/红为任务双方',
                                        map_name(self._scene), mission_name(layout["name"])), 10000)

    def follow_game_map(self) -> None:
        """恢复最近收到的游戏地图，不必等待游戏下一次换图。"""
        self._end_edit_if_active()
        self._map_locked = False
        self._set_planning_view(False)
        if self._game_scene is not None:
            self._scene = None  # 同图编辑过后也重新载入游戏方案，结束连续编辑模式
            self._on_scene(self._game_scene)
        else:
            self._set_task_scope(None)
        if self._game_image is not None:
            image, scene = self._game_image
            if scene == self._game_scene:
                self._on_map(image, scene)
        if self._game_objects_scene == self._game_scene:
            fresh = self._clock() - self._game_objects_at <= LIVE_FRAME_SEC
            objs = self._game_objects if fresh else []
            self._update_vehicle_kind(objs)
            self._render_game_objects(objs)
        self.statusBar().showMessage(tr("已切回跟随游戏地图") if self._game_image else
                                     tr("已切回跟随游戏，等待游戏地图数据"), 5000)

    def _set_planning_view(self, on: bool) -> None:
        """编辑地图库时用完整侧栏，顶部导航一直保留。"""
        self._library_mode = bool(on)
        self._refresh_ticket_eta()
        if on:
            self.map_view.clear_fire_control_guides()
        self._apply_overlay_items()
        page = "library" if on else "live"
        if self._sidebar_page != page:
            self._show_sidebar(page)
        self._sync_right_visibility()

    @mapimg.synchronized
    def rename_map(self, old: str, new: str) -> bool:
        old, new = (old or "").strip(), (new or "").strip()
        if not old or not new or mapimg.scene_path(self.data_root, old) == mapimg.scene_path(self.data_root, new):
            return False
        try:
            mapimg.rename_map_data(self.data_root, old, new)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, tr("重命名地图"), tr('改名失败：{0}', exc))
            return False
        self._rebind_map_scenes({old: new})
        if "library" in self.sidebar_pages:
            self.sidebar_pages["library"].refresh()
        self.statusBar().showMessage(tr('地图已重命名：{0} → {1}', old, new), 5000)
        return True

    def _on_new_battle(self, reason: str = "") -> None:
        self._set_vehicle_kind("unknown")
        if not self._awaiting_battle_map:
            self._reset_game_task()
        self._tickets.reset()
        self._ticket_scene = None
        self.map_view.set_ticket_eta(None)
        # 新局恢复临时暂停的自动放大，并保留已选的跟随玩家模式。
        self.map_view.reset_fit_lock()
        """对局边界统一入口: 识别池、事件面板、id 去重、告警武装全部归零，并**记下原因**。

        原因一定要留痕: 上一轮"名单莫名清空"就是因为触发条件不可查, 只能靠猜。
        现在每次清空都写进 `clear_log`(内存) + `data/diagnostic/clears.log`(磁盘)。
        """
        self._fit_pending = True
        self._game_objects = []
        self._game_objects_scene = None
        self._game_objects_at = -1e9
        self._last_live_at = -1e9
        self._cache_display_scene = None
        if not self._map_locked:
            self.map_view.on_reconnect()           # 轨迹、死亡环及旧单位不能跨局
        why = reason or self.battle.last_reason or tr("未记录原因")
        line = f"#{self.battle.battle_id} {why}"
        self.clear_log.append(line)
        if len(self.clear_log) > 50:
            del self.clear_log[:-50]
        self.event_panel.clear_feed()
        self._feed_seen.clear()
        self.alerts.reset()
        self._fresh_roster()
        self.statusBar().showMessage(tr('新一局开始（第 {0} 局）· {1}', self.battle.battle_id, why), 8000)
        log.info("清空识别池: %s", line)
        try:
            p = self.data_root / "diagnostic" / "clears.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        except OSError:
            pass                                # 诊断日志不该影响主流程

    def _maybe_show_unconnected(self):
        # 简单提示: 若 1.5s 后仍离线, 给状态栏提示
        QTimer.singleShot(1500, self._check_online)

    def _check_online(self):
        if not getattr(self, "_online", False):
            self.statusBar().showMessage(tr("未连接到战雷战术接口(localhost:8111)。请在游戏设置中开启 Live 战术接口。"), 0)

    # ---------------- Scheme ops ----------------
    def _live_task_catalog(self):
        if self._task_catalog_scene != self._game_scene:
            layouts = mapimg.read_meta(self.data_root, self._game_scene).get("public_layouts", [])
            self._task_catalog = task_layout.prepare(layouts)
            self._task_catalog_scene = self._game_scene
        return self._task_catalog

    def _reset_game_task(self):
        self._game_task = self._task_candidate = None
        self._apply_live_task()

    def _apply_live_task(self) -> None:
        if not self._map_locked and self._task != self._game_task and not self._keep_active_plan():
            self._set_task_scope(self._game_task)

    def _choose_live_task(self):
        context = (self._game_scene, self.battle.battle_id)
        tasks = {task.id: task for task in self._live_task_catalog() if task is not None}
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("本局任务"))
        dialog.resize(460, 140)
        body = QVBoxLayout(dialog)
        mode_combo = task_mode_combo()
        body.addWidget(mode_combo)
        choices = QComboBox()
        choices.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        body.addWidget(choices)

        def filter_choices():
            previous = choices.currentData()
            mode = mode_combo.currentData()
            choices.clear()
            choices.addItem(tr("自动识别"), None)
            for task in tasks.values():
                if mode == "all" or mode in task.modes:
                    label = f"{task_choice_name(task)}（{task.id}）"
                    choices.addItem(label, task.id)
                    choices.setItemData(choices.count() - 1, label, Qt.ItemDataRole.ToolTipRole)
            choices.setCurrentIndex(max(0, choices.findData(previous)))

        filter_choices()
        if self._game_task is not None:
            choices.setCurrentIndex(max(0, choices.findData(self._game_task.id)))
        mode_combo.currentIndexChanged.connect(filter_choices)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("确定"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        body.addWidget(buttons)
        ok = dialog.exec() == QDialog.DialogCode.Accepted
        choice = choices.currentData()
        dialog.deleteLater()
        if ok and not self._map_locked and context == (self._game_scene, self.battle.battle_id):
            self._game_task = tasks.get(choice)
            self._task_candidate = None
            self._set_task_scope(self._game_task)

    def _visible_schemes(self):
        ids = self._task.ids if self._task else set()
        return [scheme for scheme in self._schemes if not scheme.task_ids or not ids.isdisjoint(scheme.task_ids)]

    def _default_scheme(self):
        schemes = self._visible_schemes()
        if self._task:
            match = next((scheme for scheme in schemes if scheme.task_ids), None)
            if match is not None:
                return match
        return schemes[0] if schemes else None

    def _set_task_scope(self, task):
        self._task = task
        self._current = self._default_scheme()
        self.map_view.set_scheme(self._current)
        self._refresh_scheme_list()

    def _bind_current_task(self):
        scheme = self._current
        scene = self._scene
        if scheme is None or scene is None:
            return
        layouts = mapimg.read_meta(self.data_root, scene).get("public_layouts", [])
        tasks = {task.id: task for task in task_layout.prepare(layouts) if task is not None}
        if not tasks:
            return
        old = set(scheme.task_ids)
        dialog = QDialog(self)
        dialog.setWindowTitle(tr("绑定任务"))
        dialog.resize(460, 440)
        body = QVBoxLayout(dialog)
        hint = QLabel(tr("勾选要共用此方案的任务。全部取消勾选则设为地图通用方案。"))
        hint.setWordWrap(True)
        body.addWidget(hint)
        mode_combo = task_mode_combo()
        body.addWidget(mode_combo)
        task_list = QListWidget()
        task_list.setWordWrap(True)
        body.addWidget(task_list)
        known = set().union(*(task.ids for task in tasks.values()))
        current_id = self._task.id if self._task else None
        rows = []
        for task in tasks.values():
            bound_ids = old.intersection(task.ids)
            checked = bool(bound_ids) if old else task.id == current_id
            rows.append((task_choice_name(task), bound_ids or {task.id}, checked, task.modes))
        rows.extend((tr('已不可用的任务（{0}）', key), {key}, True, {"other"}) for key in sorted(old - known))
        mode_role = Qt.ItemDataRole.UserRole + 1
        for name, ids, checked, modes in rows:
            item = QListWidgetItem(name, task_list)
            item.setData(Qt.ItemDataRole.UserRole, sorted(ids))
            item.setData(mode_role, sorted(modes))
            item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

        def filter_tasks():
            mode = mode_combo.currentData()
            for index in range(task_list.count()):
                item = task_list.item(index)
                item.setHidden(mode != "all" and mode not in item.data(mode_role))

        mode_combo.currentIndexChanged.connect(filter_tasks)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText(tr("保存绑定"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("取消"))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        body.addWidget(buttons)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        dialog.deleteLater()
        if not accepted or self._scene != scene or not any(item is scheme for item in self._schemes):
            return
        selected = set()
        for index in range(task_list.count()):
            item = task_list.item(index)
            if item.checkState() == Qt.CheckState.Checked:
                selected.update(item.data(Qt.ItemDataRole.UserRole))
        if selected == old:
            return
        previous = scheme.task_ids
        scheme.task_ids = sorted(selected)
        if not self.persist_schemes():
            scheme.task_ids = previous
            self._refresh_scheme_list()
        elif self._current is scheme and scheme not in self._visible_schemes():
            self._set_task_scope(self._task)

    def _refresh_scheme_list(self):
        """重建方案列表并高亮当前方案(不触发切换)。"""
        self._known_scheme_ids = {scheme.id for scheme in self._schemes}
        self.scheme_list.blockSignals(True)
        self.scheme_list.clear()
        visible = self._visible_schemes()
        for s in visible:
            name = s.name + (tr(" · 已绑定任务") if s.task_ids else "")
            it = QListWidgetItem(name)
            it.setData(Qt.ItemDataRole.UserRole, s.id)
            self.scheme_list.addItem(it)
            active = s is self._current
            holder = QWidget()
            holder.setObjectName("schemeRow")
            holder.setProperty("active", active)
            holder.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
            holder.setAutoFillBackground(True)
            holder.setToolTip(name)
            layout = QHBoxLayout(holder)
            layout.setContentsMargins(4, 0, 4, 0)
            label = ElidedLabel(name)
            label.setObjectName("schemeRowName")
            label.setProperty("active", active)
            label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            layout.addWidget(label, 1)
            check = QCheckBox()
            check.setToolTip(tr("叠加显示此方案"))
            check.setAccessibleName(tr("叠加显示此方案") + " " + s.name)
            check.setChecked(active or s.id in self._shown_schemes)
            check.setEnabled(not active)
            check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            check.toggled.connect(lambda checked, sid=s.id: self._toggle_reference(sid, checked))
            layout.addWidget(check)
            self.scheme_list.setItemWidget(it, holder)
        if self._current:
            idx = next((i for i, s in enumerate(visible) if s.id == self._current.id), -1)
            if idx >= 0:
                self.scheme_list.setCurrentRow(idx)
        self.scheme_list.blockSignals(False)
        self._refresh_references()
        self.scheme_list.setVisible(bool(visible))
        self.scheme_empty.setVisible(not visible)
        self.scheme_empty.setText(tr("暂无方案") if self._scene else tr("等待地图"))
        self.scheme_actions["new"].setEnabled(self._scene is not None)
        for key in ("rename", "delete"):
            self.scheme_actions[key].setEnabled(self._current is not None)
        bind = self.scheme_actions["bind"]
        self.export_scheme_button.setEnabled(bool(self._current and (self._current.markers or self._current.areas)))
        catalog = self._live_task_catalog() if not self._map_locked and self._game_scene else []
        bind.setEnabled(self._current is not None and (self._task is not None or any(catalog)))
        self.scheme_task.setVisible(self._task is not None or bool(catalog))
        self.scheme_task.setEnabled(not self._map_locked and bool(catalog))
        self.scheme_task.setText(mission_name(self._task.name) if self._task else tr("任务未确认 · 手动选择…"))
        task_hint = mission_name(self._task.name) if self._task else tr("任务未确认")
        if catalog:
            task_hint += tr("\n点击可手动选择本局任务")
        self.scheme_task.setToolTip(task_hint)
        box = self.panels["schemes"]
        if len(visible) < len(self._schemes):
            box.set_badge(tr('显示 {0}/{1} 套', len(visible), len(self._schemes)))
            box.header.setToolTip(tr("部分方案绑定到其他任务。切换任务可查看。"))
        else:
            box.set_badge(tr('{0} 套', len(visible)))
            box.header.setToolTip("")
        self._refresh_marker_list()
        self._refresh_context()

    def _on_scheme_clicked(self, item: QListWidgetItem, _previous=None):
        """点方案列表里的某一项即切换(用户要求: 点击列表即可切换)。"""
        if item is None:
            return
        sid = item.data(Qt.ItemDataRole.UserRole)
        s = next((x for x in self._schemes if x.id == sid), None)
        if s is None or (self._current is not None and s.id == self._current.id):
            return
        self._current = s
        self.map_view.set_scheme(s)
        self._refresh_scheme_list()
        self.statusBar().showMessage(tr('已切换到方案「{0}」', s.name), 3000)

    def _refresh_marker_list(self):
        with QSignalBlocker(self.marker_list):
            self.marker_list.clear()
            if self._current:
                for marker in self._current.markers:
                    name = tr(MARKER_TYPES.get(marker.type, MARKER_TYPES["custom"])[0])
                    self._add_scheme_entry(f"[{name}] {marker.label or tr('(无标签)')}", "marker", marker.id)
                for number, area in enumerate(self._current.areas, 1):
                    self._add_scheme_entry(f"[{tr(AREA_TYPES[area.type])}] {number}", "area", area.id)
        has_entries = self.marker_list.count() > 0
        self.marker_list_label.setVisible(self._current is not None)
        self.marker_list.setVisible(has_entries)
        self.marker_list_label.setText(tr("标点与区域") if has_entries else tr("暂无标点或区域"))
        if not self._current:
            return
        self._on_map_selection_changed()
        scheme = self._current
        self.statusBar().showMessage(tr('场景: {0} | 方案: {1} | 标点 {2} | 连线 {3} | 区域 {4}', self._scene or '?', scheme.name, len(scheme.markers), len(scheme.paths), len(scheme.areas)), 0)

    def _add_scheme_entry(self, label, kind, entry_id):
        item = QListWidgetItem(label)
        item.setData(Qt.ItemDataRole.UserRole, entry_id)
        item.setData(Qt.ItemDataRole.UserRole + 1, kind)
        self.marker_list.addItem(item)

    def _scheme_entry_item(self, entry):
        if entry is None:
            return None
        items = (self.map_view._area_items if entry.data(Qt.ItemDataRole.UserRole + 1) == "area"
                 else self.map_view._marker_items)
        return items.get(entry.data(Qt.ItemDataRole.UserRole))

    def _area_list_menu(self, pos):
        from PySide6.QtWidgets import QMenu
        entry = self.marker_list.itemAt(pos)
        item = self._scheme_entry_item(entry)
        if isinstance(item, AreaItem) and not self.map_view._area_editor:
            self.marker_list.setCurrentItem(entry)
            menu = QMenu(self.marker_list)
            self.map_view.add_area_actions(menu, item.area)
            menu.exec(self.marker_list.viewport().mapToGlobal(pos))
            menu.deleteLater()

    def _on_marker_selected(self):
        with QSignalBlocker(self.map_view._scene):
            self.map_view._scene.clearSelection()
            for entry in self.marker_list.selectedItems():
                graphic = self._scheme_entry_item(entry)
                if graphic is not None:
                    graphic.setSelected(True)

    def _on_map_selection_changed(self):
        selected = set(self.map_view._scene.selectedItems())
        with QSignalBlocker(self.marker_list):
            first = -1
            for row in range(self.marker_list.count()):
                entry = self.marker_list.item(row)
                match = self._scheme_entry_item(entry) in selected
                entry.setSelected(match)
                if match and first < 0:
                    first = row
            self.marker_list.setCurrentRow(first, QItemSelectionModel.SelectionFlag.NoUpdate)

    def _toggle_reference(self, sid, checked):
        if checked:
            self._shown_schemes.add(sid)
        else:
            self._shown_schemes.discard(sid)
        self._refresh_references()

    def _refresh_references(self):
        self.map_view.set_references([s for s in self._visible_schemes()
                                      if s is not self._current and s.id in self._shown_schemes])

    def _scheme_menu(self, pos):
        item = self.scheme_list.itemAt(pos)
        if item is None:
            return
        source = next(s for s in self._schemes if s.id == item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self.scheme_list)
        targets = menu.addMenu(tr("合并到方案"))
        for scheme in self._schemes:
            if scheme is not source:
                targets.addAction(scheme.name, lambda checked=False, dest=scheme: self.map_view.apply_group(dest, source))
        targets.setEnabled(bool(targets.actions()) and bool(source.markers or source.areas))
        menu.exec(self.scheme_list.viewport().mapToGlobal(pos))
        menu.deleteLater()

    @mapimg.synchronized
    def _undo_edit(self):
        editor = self.map_view._area_editor
        if editor is not None:
            editor.undo()
            return
        index = next((i for i in reversed(range(len(self._undo_history)))
                      if self._undo_history[i][0] == self._scene), None)
        if index is None:
            return
        scene, before, after = self._undo_history[index]
        try:
            path = markers_store.scheme_file(self.data_root, scene)
            stored = markers_store.read_schemes(path) if path.exists() else []
            records = {s.id: s.to_dict() for s in stored}
            local = {s.id: s.to_dict() for s in self._schemes}
            if any(records.get(k) != value or local.get(k) != value for k, value in after.items()):
                self.statusBar().showMessage(tr("方案已变更，无法撤回这次编辑"), 4000)
                return
            for key, value in before.items():
                if value is None:
                    records.pop(key, None)
                else:
                    records[key] = value
            restored = [Scheme.from_dict(r) for r in records.values()]
            markers_store.save_schemes(self.data_root, scene, restored)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, tr("方案未保存"), str(exc))
            return
        del self._undo_history[index]
        current_id = self._current.id if self._current else None
        self._schemes = restored
        self._current = next((s for s in self._visible_schemes() if s.id == current_id), self._default_scheme())
        self.map_view.set_scheme(self._current)
        self._refresh_scheme_list()
        self.statusBar().showMessage(tr("已撤回上一次编辑"), 3000)

    def _on_marker_dbl(self, it):
        gi = self._scheme_entry_item(it)
        if gi is not None and not self.map_view._area_editor:
            self.map_view.user_adjusted_view()
            self.map_view.centerOn(gi)

    def _new_scheme(self):
        if self._scene is None:
            self.statusBar().showMessage(tr("请先进入游戏地图或在地图库选择地图，再新建方案"), 5000)
            return
        context = (self._scene, self._task)
        name, ok = QInputDialog.getText(self, tr("新建方案"), tr("方案名:"))
        if not ok or context != (self._scene, self._task):
            return
        self.create_scheme_for(self._scene or "_unknown", name or tr("未命名方案"))

    def _rename_current(self):
        scheme = self._current
        if not scheme:
            QMessageBox.information(self, tr("提示"), tr("请先选择方案。")); return
        name, ok = QInputDialog.getText(self, tr("重命名方案"), tr("方案名:"), text=scheme.name)
        if ok and name and self._current is scheme:
            scheme.name = name
            self.persist_schemes()
            self._refresh_scheme_list()

    def _delete_current(self):
        scheme = self._current
        if not scheme:
            return
        if (QMessageBox.question(self, tr("确认"), tr('删除方案 [{0}]?', scheme.name)) != QMessageBox.Yes
                or self._current is not scheme):
            return
        previous = list(self._schemes)
        self._schemes = [s for s in self._schemes if s.id != self._current.id]
        if not self.persist_schemes():
            self._schemes = previous
            return
        self._current = self._default_scheme()
        self.map_view.set_scheme(self._current)
        self._refresh_scheme_list()

    # ---------------- Public helpers for children ----------------
    def _export_scheme(self, *, chinese=False):
        if self._current is None:
            return
        from ..store.share_code import EXTENDED_PREFIX, encode
        try:
            code = encode(self._current, chinese=chinese)
        except ValueError as exc:
            QMessageBox.warning(self, tr("无法生成分享码"), str(exc))
            return
        QApplication.clipboard().setText(code)
        hint = (tr("汉字短码已复制 · 请用支持 WT5 的终端导入") if chinese else
                tr("分享码已复制 · 请用支持 WT4 的终端导入") if code.startswith(EXTENDED_PREFIX) else
                tr("分享码已复制，可粘贴发送给他人"))
        self.statusBar().showMessage(hint, 5000)

    def _import_scheme(self):
        from .share_scheme import ImportSchemeDialog
        dialog = ImportSchemeDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.saved_scheme is not None:
            scheme = dialog.saved_scheme
            self.choose_map(scheme.scene)
            # choose_map reloads the stored objects; activate that same instance for editing.
            imported = next(item for item in self._schemes if item.id == scheme.id)
            self.choose_scheme_for(scheme.scene, imported)
            hint = tr(" · 请通过「绑定任务…」为出生区选择对应任务") if any(a.type == "rally" for a in scheme.areas) else ""
            self.statusBar().showMessage(tr('已导入新方案「{0}」{1}', scheme.name, hint), 8000)
        dialog.deleteLater()

    @mapimg.synchronized
    def save_imported_scheme(self, scheme):
        scene = mapimg.resolve_scene(self.data_root, scheme.scene)
        if not mapimg.map_cache_path(self.data_root, scene).is_file():
            raise ValueError(tr("所选地图已移除，请重新选择地图并预览"))
        if self._scene == scene and self.persist_schemes() is False:
            raise ValueError(tr("当前方案尚未成功保存"))
        path = markers_store.scheme_file(self.data_root, scene)
        schemes = markers_store.read_schemes(path) if path.exists() else []
        if any(item.id == scheme.id for item in schemes):
            raise ValueError(tr("这份预览已经保存，请重新预览后再导入"))
        scheme.scene = scene
        markers_store.save_schemes(self.data_root, scene, [*schemes, scheme])

    def persist_schemes(self):
        if self._scene is None:
            return False
        try:
            canonical, schemes = self._write_current_schemes()
        except (OSError, ValueError) as exc:
            log.warning("方案未保存，保留现有文件: %s", exc)
            QMessageBox.warning(self, tr("方案未保存"), tr('{0}\n当前编辑仍留在窗口中，请先恢复文件后再保存。', exc))
            return False
        self._schemes = schemes
        if canonical != self._scene:
            self._scene = canonical
            self.map_view.set_scene_name(canonical)
        self._refresh_scheme_list()
        return True

    @mapimg.synchronized
    def _write_current_schemes(self):
        canonical = mapimg.resolve_scene(self.data_root, self._scene)
        local = {scheme.id: scheme for scheme in self._schemes}
        # Keep schemes moved here by map maintenance while honoring local deletions.
        stored = markers_store.load_schemes(self.data_root, canonical)
        combined = {scheme.id: scheme for scheme in stored
                    if scheme.id not in self._known_scheme_ids or scheme.id in local}
        combined.update(local)
        schemes = list(combined.values())
        previous_scenes = [scheme.scene for scheme in schemes]
        for scheme in schemes:
            scheme.scene = canonical
        try:
            markers_store.save_schemes(self.data_root, canonical, schemes)
        except (OSError, ValueError):
            for scheme, previous in zip(schemes, previous_scenes):
                scheme.scene = previous
            raise
        before = {s.id: s.to_dict() for s in stored}
        after = {s.id: s.to_dict() for s in schemes}
        changed = [k for k in dict.fromkeys([*before, *after]) if before.get(k) != after.get(k)]
        if changed:
            self._undo_history.append((canonical, {k: before.get(k) for k in changed},
                                       {k: after.get(k) for k in changed}))
        return canonical, schemes

    def new_scheme(self, name: str):
        self.create_scheme_for(self._scene or "_unknown", name)

    def create_scheme_for(self, scene: str | None, name: str):
        s = Scheme.fresh(scene or "_unknown", name)
        # 若 fly-by: 当前 _scene 已是该 scene 则同步加入并激活
        if scene == self._scene or (scene == "_unknown" and self._scene):
            if self._task:
                s.task_ids = [self._task.id]
            self._schemes.append(s)
            self._current = s
            self.map_view.set_scheme(s)
            self.persist_schemes()
            self._refresh_scheme_list()
        else:
            # 写入目标地图的方案库, 但不切换
            others = markers_store.load_schemes(self.data_root, scene)
            others.append(s)
            markers_store.save_schemes(self.data_root, scene, others)
            QMessageBox.information(self, tr("提示"), tr('已为地图 [{0}] 新建方案 [{1}]。', scene, name))

    def choose_scheme_for(self, scene: str | None, scheme: Scheme | None):
        if scheme is not None and scheme.task_ids and (scene != self._scene or scheme not in self._visible_schemes()):
            return
        # 来自 picker 的应用
        if scene != self._scene:
            # 切换到目标场景(罕见: 重命名后)
            self._scene = scene
            self.map_view.set_scene_name(scene)
            self._schemes = markers_store.load_schemes(self.data_root, scene)
        self._current = scheme
        self.map_view.set_scheme(scheme)
        self._refresh_scheme_list()

    def rename_scene(self, old: str, new: str) -> bool:
        return self.rename_map(old, new)

    def _open_picker(self):
        from .scheme_picker import SchemePicker
        dlg = SchemePicker(self, self._scene or "_unknown", self._visible_schemes(), self)
        dlg.exec()

    # ---------------- shutdown ----------------
    def closeEvent(self, e):
        if self.update_button is not None and not self.update_button.can_close():
            e.ignore()
            QTimer.singleShot(400, self.close)
            return
        self._startup_library_timer.stop()
        self._library_timer.stop()
        self._library_cancel.set()
        self._library_executor.shutdown(wait=False, cancel_futures=True)
        self.phone_mirror.stop()
        self._roster_timer.stop()
        self.poller.stop()
        if not self.poller.wait(3000):
            e.ignore()                         # 请求尚在结束时不销毁仍工作的 QThread
            QTimer.singleShot(250, self.close)
            return
        if self._combat_overlay is not None:
            self._combat_overlay.close()
        super().closeEvent(e)
