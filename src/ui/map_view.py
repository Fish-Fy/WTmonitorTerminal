"""地图视图: 底图 + 地图对象(玩家/敌友/出生点/占领区) + 轨迹/击杀/死亡标记 + 自定义标点。

坐标系: 归一化 [0,1] 直接与 /map.img 像素对齐(像素 = 坐标 * 图像尺寸)。
右键编辑战术标点、连线和区域；Shift+左键快速加点或连接节点。

敌方红色单位(有意保留, 勿当外挂删掉): /map_obj.json 只在目标被我方侦察点亮后才下发它,
即游戏内小地图本来就能看到这个点; 这里只是把已有信息重绘到大尺寸底图上, 不产生额外视野。

阵营与名字为什么接不起来(实测穷尽 8111 的全部端点): /map_obj.json **有阵营但无名字**
(color 蓝/红/黄 + 机型类别), /hudmsg **有名字与机型但无阵营**(mode/sender 恒空、enemy 恒 false),
/gamechat 实测返回空数组, 端点全集里没有 /players 之类的名册接口。
⇒ 无法直接读出"某玩家是哪一阵营", 只能靠击杀文案构成的交战图反推(见 src/agent/roster.py)。
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from PySide6.QtCore import QSignalBlocker, Qt, Signal, QRect, QRectF, QPointF, QTimer
from PySide6.QtGui import (
    QBrush, QColor, QCursor, QFont, QFontDatabase, QImage, QPen, QPixmap, QAction, QKeySequence, QPainter, QPolygonF, QPainterPath,
)
from PySide6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QGraphicsPixmapItem, QGraphicsItem,
    QGraphicsPolygonItem, QGraphicsLineItem, QGraphicsRectItem,
    QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsSimpleTextItem, QMenu, QInputDialog, QMessageBox,
    QStyle, QStyleOptionGraphicsItem, QRubberBand, QGraphicsItemGroup, QComboBox, QLabel, QPushButton, QDialog, QCheckBox,
)

from ..i18n import tr
from ..store import scheme_edit
from ..store.models import Marker, PathLink, Scheme, MARKER_TYPES, MARKER_TYPE_CHOICES, HIDDEN_TYPES
from ..store.areas import AREA_SHAPES, AREA_TYPES, MAX_AREAS, Area
from ..agent.mapobj import MapObj, TEAM_COLORS
from ..agent.threatwatch import RADIUS_MAX, RADIUS_MIN   # 判定与绘制共用同一个半径下限
from ..agent.fitting import (
    ACTIVITY_FILL_RANGE, DEFAULT_ACTIVITY_FILL, DEFAULT_FOLLOW_ZOOM, FOLLOW_ZOOM_RANGE,
    activity_bbox, fit_square,
)
from ..agent import shapes
from ..agent.teammate_tracks import (
    Observation, Position, TeammateDeathMatcher, TeammateIconLossTracker, match_teammates,
)
from ..agent.hudmsg import HudEvent
from ..agent.telemetry import Attitude, StateSample
from .overlay import ITEM_LABELS, MapOverlay
from .settings import DisplaySettings
from .air_target_icons import (
    ground_symbol_kind, make_heading_ring, make_spawn_item, make_target_item, target_kind,
)
from .fire_control_marker import FireControlMarker, is_fire_control
from ..agent.fire_control_guidance import course_aligned, runway_approach
from .area_editor import AreaEditor
from .area_items import AreaBoundaryOverlay, AreaItem
from .scheme_path_item import SchemePathItem, visible_link_pairs


if TYPE_CHECKING:
    from .main_window import MainWindow

AREA_DRAW_MODES = (
    ("circle", "圆形（拖拽）"),
    ("circle_three_points", "圆形（三点定圆）"),
    ("polygon", AREA_SHAPES["polygon"]),
)

# 视口横幅: 游戏没下发地图对象时, 把原因写在**地图上**而不是只写状态栏
# (真机 2026-10-23: 部署/阵亡/回放界面 `/map_obj.json` 返回 0 字节, 官方 8111 网页同样空白)
BANNER_STALE = "非实时 · 缓存画面"
BANNER_EMPTY = "等待实时数据"
ANNOTATION_OUTLINE_PX = 10.0
ANNOTATION_FADE_PX = 42.0
PLAYER_LAYER = 42.0
TEAMMATE_TRAIL_SECONDS = 45.0
TEAMMATE_FRAME_GAP_SECONDS = 2.0
TEAMMATE_DEATH_SECONDS = 9 * 60
NEARBY_DEATH_SECONDS = 90
TEAMMATE_DEATH_RADIUS = 0.025  # 归一化地图距离，和当前视角无关
SCHEME_LINK_HIT_RADIUS_PX = 12

# 对象 type -> (显示名, 形状: tri/diamond/spawn/segment)
OBJ_TYPES: dict[str, tuple[str, str]] = {
    "airfield":             ("机场",     "segment"),
    "capture_zone":         ("占领区",   "segment"),
    "respawn_base_tank":    ("坦出生点", "spawn"),
    "respawn_base_fighter": ("战出生点", "spawn"),
    "respawn_base_bomber":  ("轰出生点", "spawn"),
    "respawn_base_ucav":    ("无人机出生", "spawn"),
    "ground_model":         ("地面单位", "diamond"),
    "bombing_point":        ("轰炸战区", "target"),
    "defending_point":      ("防守战区", "shield"),
    "aircraft":             ("空中单位", "tri"),
}


def _annotation_opacity(distance: float, inner: float, outer: float) -> float:
    if distance >= outer:
        return 1.0
    if distance <= inner:
        return 0.0
    fraction = (distance - inner) / (outer - inner)
    return round(fraction * fraction * (3 - 2 * fraction), 2)


class MarkerItem(QGraphicsPixmapItem):
    """单个标点图元。

    可拖动；**拖动后要把新位置写回 marker 并重画连线**（用户 2026-10-12 报"点之间的连线是
    创建后定死的, 点移动位置不能自动重新连接" —— 以前只改了图元的 pos, marker.cx/cy 与线都没动）。
    """

    def __init__(self, marker: Marker, view: "MapView"):
        pm = view.make_marker_pixmap(marker.type)
        super().__init__(pm)
        self.marker = marker
        self._view = view
        self._fill_opacity = 1.0
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setZValue(45.0)
        self.setToolTip(self._tip())

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged and value:
            self._view.start_selection_blink()
        return super().itemChange(change, value)

    def set_fill_opacity(self, opacity):
        if opacity != self._fill_opacity:
            self._fill_opacity = opacity
            self.update()

    def paint(self, painter, option, widget=None):
        selected = self.isSelected()
        if not selected and self._fill_opacity == 1.0:
            super().paint(painter, option, widget)
            return
        painter.save()
        if selected:
            option = QStyleOptionGraphicsItem(option)
            option.state &= ~QStyle.StateFlag.State_Selected
            if self._view._selection_dim:
                painter.setOpacity(painter.opacity() * .28)
        opacity = painter.opacity()
        if self._fill_opacity > 0.0:
            painter.setOpacity(opacity * self._fill_opacity)
            super().paint(painter, option, widget)
        if self._fill_opacity < 1.0:
            painter.setOpacity(opacity)
            painter.drawPixmap(self.offset(), self._view.make_marker_pixmap(self.marker.type, outline=True))
        painter.restore()

    # ---- 拖动后的坐标回写(由 MapView.mouseReleaseEvent 统一触发) ----
    def sync_marker_pos(self) -> bool:
        """把图元当前位置写回归一化坐标; 有变化返回 True。

        ⚠ 这里**不用** `QGraphicsItem.itemChange` —— 实测(2026-10-12) PySide6 下收不到
        `ItemPositionHasChanged`: 拖完 `marker.cx/cy` 一直没变, 连线也就永远定死在创建时的
        端点上。改为在 `MapView.mouseReleaseEvent`(松手必然触发)里统一扫描回写。
        """
        v = self._view
        c = self.sceneBoundingRect().center()     # 含图元 scale, 取真实几何中心
        cx = max(0.0, min(1.0, c.x() / max(v._img_w, 1.0)))
        cy = max(0.0, min(1.0, c.y() / max(v._img_h, 1.0)))
        if abs(self.marker.cx - cx) < 1e-9 and abs(self.marker.cy - cy) < 1e-9:
            return False              # _relayout_markers 也会走到这, 值没变就别白重画一遍
        self.marker.cx, self.marker.cy = cx, cy
        self.setToolTip(self._tip())
        return True

    def _tip(self) -> str:
        tname, _, _ = MARKER_TYPES.get(self.marker.type, (tr("自定义"), "#888", "dot"))
        lbl = f"{self.marker.label} " if self.marker.label else ""
        note = f"\n{self.marker.note}" if self.marker.note else ""
        return f"[{tr(tname)}] {lbl}({self.marker.cx:.3f},{self.marker.cy:.3f}){note}"


class TrackState:
    """玩家的轨迹状态。"""
    __slots__ = ("points", "dead", "last_seen", "seg_items")

    def __init__(self):
        self.points: list[tuple[float, float]] = []   # 像素点
        self.dead: bool = False
        self.last_seen: tuple[float, float] | None = None
        self.seg_items: list[tuple[QGraphicsLineItem, ...]] = []  # 每段的描边和线芯一起管理


@dataclass(slots=True)
class TeammateDeath:
    position: Position
    expires_at: float


def _nearby_death_groups(deaths: list[TeammateDeath]) -> list[list[Position]]:
    """把由相邻阵亡点连起来的标记画在一起。"""
    remaining = [death.position for death in deaths]
    groups = []
    while remaining:
        group = [remaining.pop()]
        for point in group:
            distant = []
            for other in remaining:
                if math.dist(point, other) <= TEAMMATE_DEATH_RADIUS:
                    group.append(other)
                else:
                    distant.append(other)
            remaining = distant
        groups.append(group)
    return groups


@cache
def _teammate_death_count_font() -> QFont:
    font_path = Path(__file__).resolve().parents[2] / "assets/fonts/rajdhani/Rajdhani-Bold.ttf"
    font_id = QFontDatabase.addApplicationFont(str(font_path))
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    font = QFont(families[0] if families else "Arial")
    font.setPixelSize(16)
    font.setBold(True)
    return font


class MapView(QGraphicsView):
    active_scheme_changed = Signal()
    # 归一化半径(= 可视区边长 × 比例, 换图时重算)变化 → 通知主窗口, 判定与画的是同一个圈
    guard_radius_changed = Signal(float)
    overlay_items_changed = Signal()      # 叠加显示开关变了 → 主窗口把开关存进 ui_state.json
    display_settings_changed = Signal()
    interaction_changed = Signal()

    def __init__(self, win: "MainWindow", parent=None):
        super().__init__(parent)
        self.win = win
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self._pan_position = None
        self._select_origin = None
        self._band = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
        self._clipboard = None
        self._menu_position = None
        self._references = []
        self._reference_group = None
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._scene.setItemIndexMethod(QGraphicsScene.NoIndex)

        self._base: QGraphicsPixmapItem | None = None
        self._img_w = 512.0
        self._img_h = 512.0
        self._marker_items: dict[str, MarkerItem] = {}
        self._selection_dim = True
        self._selection_timer = QTimer(self)
        self._selection_timer.setInterval(450)
        self._selection_timer.timeout.connect(self._blink_selected_markers)
        self._area_items: dict[str, AreaItem] = {}
        self._area_boundaries = AreaBoundaryOverlay()
        self._area_boundaries.setZValue(PLAYER_LAYER + 1.0)
        self._scene.addItem(self._area_boundaries)
        self._area_editor: AreaEditor | None = None
        self._area_press = False
        self._path_items: list[SchemePathItem] = []
        self._scene_name: str | None = None
        self._scheme: Scheme | None = None

        # 地图对象图元(每帧重建)
        self._obj_items: list = []
        self._zone_sweeps: list[tuple[QGraphicsPathItem, QPainterPath, QRectF]] = []
        self._zone_sweep_timer = QTimer(self)
        self._zone_sweep_timer.setInterval(50)
        self._zone_sweep_timer.timeout.connect(self._animate_zone_sweeps)
        self._guard_item: QGraphicsEllipseItem | None = None
        self._fire_control_items: list[FireControlMarker] = []
        self._fire_control_guides: list[QGraphicsLineItem] = []
        self._fire_control_red = True
        self._fire_control_timer = QTimer(self)
        self._fire_control_timer.setInterval(1000)
        self._fire_control_timer.timeout.connect(self._blink_fire_control)
        self._player_obj: MapObj | None = None
        self._last_player_xy: tuple[float, float] | None = None
        self._flight_position: tuple[float, float] | None = None
        self._flight_delta: tuple[float, float] | None = None
        # 这一帧对象是不是"没有实时数据"(缓存的静态要素 / 上一帧保留) ⇒ 淡显, 不冒充实时
        self._objs_stale: bool = False

        # 玩家轨迹与仅依赖相邻帧的队友轨迹分别保存。
        self._tracks: dict[str, TrackState] = {}
        self._teammate_icon_losses = TeammateIconLossTracker()
        self._teammate_death_matcher = TeammateDeathMatcher()
        self._teammate_positions: list[Observation] = []
        self._teammate_frame_at: float | None = None
        self._teammate_segments: deque[tuple[float, Position, Position]] = deque()
        self._teammate_trail_items: list[QGraphicsPathItem] = []
        for color, width, layer in ((QColor(14, 21, 32, 205), 3.2, 8.8),
                                    (QColor(113, 170, 255, 235), 1.6, 8.9)):
            pen = QPen(color, width)
            pen.setCosmetic(True)
            pen.setCapStyle(Qt.RoundCap)
            item = self._scene.addPath(QPainterPath(), pen, QBrush(Qt.NoBrush))
            item.setZValue(layer)
            item.setAcceptedMouseButtons(Qt.NoButton)
            self._teammate_trail_items.append(item)
        self._teammate_trail_timer = QTimer(self)
        self._teammate_trail_timer.setInterval(200)
        self._teammate_trail_timer.timeout.connect(self._expire_teammate_trail)

        # 自己的阵亡标记保留到清场，队友标记按每次阵亡独立计时。
        self._event_items: list = []
        self._teammate_deaths: list[TeammateDeath] = []
        self._teammate_death_items: list[QGraphicsPathItem] = []
        self._teammate_death_timer = QTimer(self)
        self._teammate_death_timer.setSingleShot(True)
        self._teammate_death_timer.setTimerType(Qt.PreciseTimer)
        self._teammate_death_timer.timeout.connect(self._expire_teammate_deaths)

        # 设置(由主窗口同步)
        self.settings = DisplaySettings()

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._ctx)

        self._zoom = 1.0
        self._last_objs: list[MapObj] = []      # 最近一帧地图对象, 供"可活动区域"算包围盒
        self._public_layout = False
        self._fit_locked = False                # 用户手动调过视角后, 自动 fit 不再抢
        self._aircraft_active = False           # 由已确认的本人载具类型更新
        self.air_map_active = False             # 当前显示的底图类型独立于本人载具
        self._fit_box: tuple[float, float, float] | None = None   # 已算好的 (中心x,中心y,可视边长)
        self.activity_fill = DEFAULT_ACTIVITY_FILL
        self.activity_edge_pan = True
        self.follow_zoom = DEFAULT_FOLLOW_ZOOM
        self._strip_h = 0                       # 地图下方留给叠加仪表的余量高度(px)
        self._unit_prev: list[tuple[float, float, str, str]] = []   # 上一帧单位位置(算路径切线用)
        self._unit_current: list[tuple[float, float, str, str]] = []
        # 警戒圈(威胁告警"附近"半径的可视化, 用户可选)
        # 用户定稿: 格子大小每张图都不同 ⇒ 不做公里换算, 按"**可视区边长 × 比例**"生成。
        # 好处: 圈的屏幕直径 = 2 × 半径比例 × 视口边长, 与底图分辨率(2048²/4096²)、
        # 换图换局、地图缩放全都无关 —— 换图后不再"突然变大变小"。
        self._guard_ratio: float = 0.10    # 面板上那个比例
        self._guard_radius: float = 0.10   # 实际归一化半径 = side × ratio(换图时重算)
        self._connect_from: str | None = None   # 点选连线的起点 marker id(用户 2026-10-13)
        self._last_marker_type: str | None = None
        self._quick_press = None
        self._editor_press = False

        # 叠加层(视口子控件): 姿态仪在右上角, 仪表栈在左上角, 不随缩放变形
        self.overlay = MapOverlay(getattr(win, "cfg", None), parent=self.viewport())
        self.overlay.lower()

        # 视口内状态横幅: 明确告诉用户"这帧为什么没有图标"(游戏没下发数据),
        # 以前只写状态栏, 用户根本看不到, 于是每次都被当成"程序坏了"。见 _set_banner。
        self._banner = QLabel(self.viewport())
        self._banner.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._banner.setStyleSheet(
            "QLabel{background:rgba(0,0,0,160);color:#ffd60a;"
            "padding:4px 10px;border-radius:6px;font-size:12px;}")
        self._banner.setVisible(False)
        self._banner_text: str = ""        # 横幅内容(不看控件可见性, 便于测试与逻辑判断)

        self._ticket_eta = QLabel(self.viewport())
        self._ticket_eta.setObjectName("ticketEta")
        self._ticket_eta.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._ticket_eta.setStyleSheet(
            "QLabel {background:rgba(12,17,24,220);color:#eaf1f9;"
            "border:1px solid #405165;border-radius:7px;"
            "padding:4px 10px;font-size:13px;font-weight:600;}")
        self._ticket_eta.hide()

    def set_ticket_eta(self, seconds: int | None) -> None:
        label = self._ticket_eta
        if seconds is None:
            label.hide()
            return
        minutes, seconds = divmod(seconds, 60)
        text = tr("预计结束 {0:02d}:{1:02d}", minutes, seconds)
        if text == label.text() and not label.isHidden():
            return
        label.setText(text)
        label.adjustSize()
        label.show()
        self._place_ticket_eta()

    def _place_ticket_eta(self) -> None:
        label = getattr(self, "_ticket_eta", None)
        if label is not None and not label.isHidden():
            label.move(max(0, (self.viewport().width() - label.width()) // 2), 8)
            label.raise_()

    # ---- "没有实时数据"的横幅 ----
    def _set_banner(self, text: str | None) -> None:
        b = getattr(self, "_banner", None)
        if b is None:
            return
        self._banner_text = str(text or "")
        if text:
            b.setText(text)
            b.setVisible(True)
            b.adjustSize()
            self._place_banner()
        else:
            b.setVisible(False)

    def _place_banner(self) -> None:
        b = getattr(self, "_banner", None)
        if b is None or not b.isVisible():
            return
        vp = self.viewport()
        b.adjustSize()
        # 放**底部居中**: 顶部左右两侧分别被仪表栈(左)和姿态仪(右)占着, 会撞在一起
        x = max(8, (vp.width() - b.width()) // 2)
        y = max(8, vp.height() - b.height() - 12)
        b.move(x, y)
        b.raise_()

    def banner_text(self) -> str:
        """当前横幅文字(测试用; 空表示没有提示)。"""
        return getattr(self, "_banner_text", "")

    # ---- 叠加层接口 ----
    def set_attitude(self, att: Attitude | None) -> None:
        self.overlay.set_attitude(att)

    def set_telemetry(self, st: StateSample | None) -> None:
        self.overlay.set_telemetry(st)

    def set_online(self, online: bool) -> None:
        self.overlay.set_online(online)
        if not online:
            self._record_flight_position(None)
            self.clear_fire_control_guides()

    def _ratio_to_radius(self, ratio: float) -> float:
        """比例(占可视区边长) → 归一化半径。还没 fit 过就按整张图算。"""
        side = self._square_viewport() / max(self._zoom * self._img_w, 1e-6) if self._base else 1.0
        return min(RADIUS_MAX, max(RADIUS_MIN, float(ratio) * max(side, 1e-6)))

    def set_guard_ratio(self, ratio: float) -> None:
        """面板上的"警戒比例" → 圈 = **可视区边长 × 比例** ⇒ 屏幕大小恒定(用户定稿)。

        格子大小逐图不同，所以按可视区计算。屏幕直径 = 2 × 比例 × 视口边长，
        与底图分辨率、换图换局、地图缩放全部无关。
        """
        self._guard_ratio = min(0.50, max(0.01, float(ratio)))
        self.set_guard_radius(self._ratio_to_radius(self._guard_ratio))

    def set_guard_radius(self, r: float) -> None:
        """立即按归一化半径重画，并回传判定使用的同一个半径。"""
        self._guard_radius = min(RADIUS_MAX, max(RADIUS_MIN, float(r)))
        if self._last_objs:
            self.set_map_objects(self._last_objs, record_tracks=False, stale=self._objs_stale,
                                 public_layout=self._public_layout)
        self.guard_radius_changed.emit(self._guard_radius)

    def _pin_overlay(self) -> None:
        """把叠加层钉回视口原位。

        QGraphicsView::scrollContentsBy 内部走 viewport()->scroll(), 而 QWidget::scroll()
        会**连带移动 viewport 的子控件** —— 不重新钉住, HUD 就跟着地图一起漂走:
        开"跟随玩家"时地图每帧都在滚, 姿态仪会越飞越远直到看不见(实测被推到 (-291,-351))。
        """
        ov = getattr(self, "overlay", None)
        if ov is None:
            return
        vp = self.viewport()
        geo = QRect(0, 0, vp.width(), vp.height())
        if ov.geometry() != geo:
            ov.setGeometry(geo)
        if hasattr(ov, "set_strip"):
            ov.set_strip(self.hud_strip_height())    # 下方余量实时同步给仪表条
        ov.update()
        self._place_banner()                         # 状态横幅同样是视口子控件, 要一起钉住
        self._place_ticket_eta()

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._pin_overlay()               # 每次滚动都钉回来(跟随玩家时是 5Hz)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._pin_overlay()
        if self._area_editor:
            return
        # 窗口尺寸变了 => 正方形裁剪与缩放要按新尺寸重算(否则 fit 是按旧边长算的, 视口不再方正)
        if self.settings.follow_player:
            self.refresh_display()
        elif self._fit_box is not None and self.activity_fit_enabled():
            self._apply_fit()
        elif self._base is not None and not self._fit_locked:
            self.fit_whole_map()
        elif self._base is not None:
            self.set_guard_radius(self._ratio_to_radius(self._guard_ratio))

    # ---- 可活动区域自适应 (K-3) ----
    def user_adjusted_view(self) -> None:
        """用户手动缩放/拖动过 => 自动 fit 让位, 不再抢视角。"""
        self._fit_locked = True
        self.settings.follow_player = False
        self.display_settings_changed.emit()

    def reset_fit_lock(self) -> None:
        """清除上一张地图的活动范围，保留已选的跟随玩家模式。"""
        self._fit_locked = self.settings.follow_player
        self._fit_box = None                     # 新任务不能复用上一局/另一张图的范围
        self.display_settings_changed.emit()

    def set_follow_player(self, on: bool) -> None:
        self.settings.follow_player = bool(on)
        if on:
            self._fit_locked = True
            self.refresh_display()
        self.display_settings_changed.emit()

    def set_activity_fill(self, ratio: float) -> None:
        low, high = ACTIVITY_FILL_RANGE
        self.activity_fill = min(high, max(low, ratio))

    def set_activity_edge_pan(self, enabled: bool) -> None:
        if enabled != self.activity_edge_pan:
            self.activity_edge_pan = enabled
            self._apply_fit()

    def set_follow_zoom(self, multiplier: float) -> None:
        low, high = FOLLOW_ZOOM_RANGE
        self.follow_zoom = min(high, max(low, multiplier))
        if self.settings.follow_player:
            self.refresh_display()

    def _update_follow_scale(self) -> None:
        if self._base is None:
            return
        scale = self.follow_zoom * self._square_viewport() / self._img_w
        if scale == self._zoom and self._strip_h == 0:
            return
        self.resetTransform()
        self.scale(scale, scale)
        self._zoom = scale
        self._strip_h = 0
        self._rebuild_marker_sizes()
        # 本帧会按新缩放画圈，告警判定使用同一个归一化半径。
        self._guard_radius = self._ratio_to_radius(self._guard_ratio)
        self.guard_radius_changed.emit(self._guard_radius)
        self._pin_overlay()

    def show_whole_map(self) -> None:
        self.user_adjusted_view()
        self.fit_whole_map()

    def end_editing(self) -> None:
        self.cancel_area_edit()
        self.cancel_connect()
        self._hint(tr("已结束编辑"), 3000)

    def has_active_plan_edit(self) -> bool:
        return bool(self._area_editor or self._connect_from
                    or self._quick_press is not None
                    or isinstance(self._scene.mouseGrabberItem(), MarkerItem))

    def toggle_activity_fit(self) -> None:
        """手动开关「自动放大地图」。

        用户 2026-10-13 报"自动放大没生效"：只要**拖过/缩放过一次**地图，user_adjusted_view()
        就把 _fit_locked 永久置真（这是有意的 —— 别跟用户抢视角），而同图连打两局不会触发
        _on_scene，于是它一直停着。这里给一个能随手重开的入口；换局(_on_new_battle)也会自动解锁。
        """
        if not self.activity_fit_available():
            return
        if self._fit_locked:
            self.settings.follow_player = False
            self.reset_fit_lock()
            self.fit_activity_area()
            self._hint(tr("已开启自动放大地图"), 3000)
        else:
            self._fit_locked = True
            self._hint(tr("已暂停自动放大地图"), 3000)
        self.display_settings_changed.emit()

    def activity_fit_enabled(self) -> bool:
        """当前是否启用自动放大地图。"""
        return not self._fit_locked and self._area_editor is None and self.activity_fit_available()

    def activity_fit_available(self) -> bool:
        return not (self._aircraft_active or self.air_map_active)

    def set_air_map_active(self, active: bool) -> None:
        if active == self.air_map_active:
            return
        restore_whole = active and self.activity_fit_enabled() and self._fit_box is not None
        self.air_map_active = active
        self._fit_box = None
        if restore_whole:
            self.fit_whole_map()
        self.display_settings_changed.emit()

    def set_aircraft_active(self, active: bool) -> None:
        """驾驶飞机时停用自动放大并隐藏阵亡标记。"""
        active = bool(active)
        if active == self._aircraft_active:
            return
        restore_whole = active and self.activity_fit_enabled() and self._fit_box is not None
        self._aircraft_active = active
        self._refresh_death_marker_visibility()
        self._fit_box = None
        if restore_whole:
            self.fit_whole_map()
        self.display_settings_changed.emit()

    def _square_viewport(self) -> int:
        """可视正方形区域的边长(px)：不再用 viewportMargins 裁方（那样会留下上下空白）。"""
        vp = self.viewport()
        return max(1, min(vp.width(), vp.height()))

    def hud_strip_height(self) -> int:
        """地图下方留给叠加仪表的余量高度（px）。宽视口时为 0。"""
        return int(getattr(self, "_strip_h", 0))

    def px_to_scene(self, screen_px: float) -> float:
        """屏幕像素 -> 场景单位。

        图元尺寸必须**屏幕恒定**：底图实测 2048²、fit 后缩放 ≈0.46，若按场景单位给 7~11px，
        屏幕上只剩 3~5px —— 这就是"占领区/队友图标太小"的根因。官方前端同样是屏幕像素
        （玩家箭头 l=25、图标 bold 18pt）。
        """
        return float(screen_px) / max(float(self._zoom), 1e-6)

    def activity_points(self) -> list[tuple[float, float]]:
        """当前任务出生点与占领区；历史缓存只供淡显，不用于判断任务范围。"""
        from ..agent.fitting import mission_activity_points
        return [] if self._objs_stale else mission_activity_points(self._last_objs)

    def _pan_activity_view(self, px: float, py: float) -> None:
        if (not self.activity_edge_pan or self._fit_box is None or not self.activity_fit_enabled()
                or not (0 <= px <= self._img_w and 0 <= py <= self._img_h)):
            return
        # 等玩家进入边缘区才平移，滚动条会把视图限制在底图范围内。
        viewport = self.viewport()
        margin = self._square_viewport() * 0.15
        position = self.viewportTransform().map(QPointF(px, py))
        dx = round(position.x() - min(max(position.x(), margin), viewport.width() - margin))
        dy = round(position.y() - min(max(position.y(), margin), viewport.height() - margin))
        horizontal, vertical = self.horizontalScrollBar(), self.verticalScrollBar()
        if dx:
            horizontal.setValue(horizontal.value() + dx)
        if dy:
            vertical.setValue(vertical.value() + dy)

    def fit_activity_area(self) -> bool:
        """按任务静态要素适配；范围不变时不重复移动视角。"""
        if not self.activity_fit_enabled() or self._base is None:
            return False
        pts = self.activity_points()
        box = activity_bbox(pts)
        if box is None:
            return False                      # 没有图内元素: 保持现状, 下一帧再试
        cx, cy, side = fit_square(box, self.activity_fill)
        # 单个占领点/重合点不足以确定任务范围，等后续出生点；紧凑任务用下限而非永远拒绝。
        if len(pts) < 2:
            return False
        side = min(max(side, 0.06), 1.0)
        cx = min(max(cx, side / 2.0), 1.0 - side / 2.0)
        cy = min(max(cy, side / 2.0), 1.0 - side / 2.0)
        target = (cx, cy, side)
        if self._fit_box is not None and all(abs(a - b) < 1e-5 for a, b in zip(target, self._fit_box)):
            return True
        self._fit_box = target
        return self._apply_fit()

    def _apply_fit(self, *, whole_map: bool = False) -> bool:
        """正方形可视区**顶部对齐**：上方不留白，多余高度全部落在下方当 HUD 条。

        用户要求：地图默认向上置顶，下面那块给叠加仪表（姿态仪、高度计…）。
        窗口尺寸一变就重算（否则是按旧边长算的，元素会掉到框外）。
        """
        if (not whole_map and not self.activity_fit_enabled()) or self._fit_box is None or self._base is None:
            return False
        cx, cy, side = self._fit_box
        vp = self.viewport()
        vw, vh = max(1, vp.width()), max(1, vp.height())
        square = min(vw, vh)
        scale = square / (side * max(self._img_w, 1.0))
        if scale <= 0:
            return False
        self.setViewportMargins(0, 0, 0, 0)          # 清掉旧版"裁方"留下的边距
        self.resetTransform()
        self.scale(scale, scale)
        world_top = cy - side / 2.0
        visible_h = vh / scale / max(self._img_h, 1.0)   # 视口高度换算成归一化高度
        self.centerOn(cx * self._img_w, (world_top + visible_h / 2.0) * self._img_h)
        self._strip_h = max(0, vh - square)
        self._zoom = float(self.transform().m11())
        self._rebuild_marker_sizes()          # 标点尺寸随缩放而变(封顶), 尺寸真变了才重建
        # 可视区边长变了 ⇒ 按"边长 × 比例"重算半径(圈的屏幕大小因此恒定), 并同步判定
        self._guard_radius = self._ratio_to_radius(getattr(self, "_guard_ratio", 0.10))
        if self._last_objs:
            self.set_map_objects(self._last_objs, record_tracks=False, stale=self._objs_stale,
                                 public_layout=self._public_layout)
        self._pin_overlay()
        self.guard_radius_changed.emit(self._guard_radius)
        return True

    def fit_whole_map(self) -> None:
        """完整底图顶部对齐；不把整图冒充已识别的任务活动区。"""
        previous = self._fit_box
        try:
            self._fit_box = (0.5, 0.5, 1.0)
            self._apply_fit(whole_map=True)
        finally:
            self._fit_box = previous

    # ---- 缩放 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton:
            self._pan_position = e.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            e.accept()
            return
        if e.button() == Qt.MouseButton.RightButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            e.accept()
            return
        if self._area_editor and e.button() == Qt.MouseButton.LeftButton:
            self._area_press = True
            self._area_editor.press(e.position().toPoint())
            e.accept()
            return
        if e.button() == Qt.MouseButton.LeftButton and getattr(
                e, "modifiers", lambda: Qt.KeyboardModifier.NoModifier)() & Qt.KeyboardModifier.ShiftModifier:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._quick_press = e.position().toPoint()
            e.accept()
            return
        if self._connect_from and e.button() == Qt.MouseButton.LeftButton:
            self._editor_press = True
            # 点选连线: 直接点地图上的目标标点即可(比从"点1 (a1b2c3)"这种列表里挑直观得多)
            it = self.itemAt(e.position().toPoint())
            if isinstance(it, MarkerItem) and it.marker.id != self._connect_from:
                src, self._connect_from = self._connect_from, None
                self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
                self._add_path(src, it.marker.id)
                self.interaction_changed.emit()
            e.accept()
            return
        if e.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            pos = e.position().toPoint()
            if not e.modifiers() & Qt.KeyboardModifier.ControlModifier and isinstance(self.itemAt(pos), MarkerItem):
                super().mousePressEvent(e)
                return
            self._select_origin = pos
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        pos = e.position().toPoint()
        if self._pan_position is not None:
            delta = pos - self._pan_position
            self._pan_position = pos
            if not delta.isNull():
                self.user_adjusted_view()
                for bar, amount in ((self.horizontalScrollBar(), delta.x()), (self.verticalScrollBar(), delta.y())):
                    bar.setValue(bar.value() - amount)
            e.accept()
            return
        if self._select_origin is not None:
            if (pos - self._select_origin).manhattanLength() > 4:
                self._band.setGeometry(QRect(self._select_origin, pos).normalized())
                self._band.show()
            e.accept()
            return
        if self._area_editor:
            self._area_editor.move(pos)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.MiddleButton:
            self._pan_position = None
            self.viewport().unsetCursor()
            e.accept()
            return
        if e.button() == Qt.MouseButton.LeftButton and self._select_origin is not None:
            origin, self._select_origin = self._select_origin, None
            pos = e.position().toPoint()
            self._band.hide()
            box = (pos - origin).manhattanLength() > 4
            if box:
                hits = [it for it in self.items(QRect(origin, pos).normalized()) if self._editable_annotation(it)]
            else:
                annotations = [it for it in self.items(pos) if self._editable_annotation(it)]
                item = self._annotation_at(pos, annotations)
                hits = [item] if item is not None else []
            toggle = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
            with QSignalBlocker(self._scene):
                if not toggle:
                    self._scene.clearSelection()
                for it in hits:
                    it.setSelected(not (toggle and it.isSelected()))
            self._scene.selectionChanged.emit()
            e.accept()
            return
        if e.button() == Qt.MouseButton.LeftButton and self._area_press:
            self._area_press = False
            if self._area_editor:
                self._area_editor.release(e.position().toPoint())
            e.accept()
            return
        if e.button() == Qt.MouseButton.LeftButton and self._quick_press is not None:
            pressed, self._quick_press = self._quick_press, None
            e.accept()
            if (e.position().toPoint() - pressed).manhattanLength() <= 4:
                self.quick_marker(e.position().toPoint())
            return
        if e.button() == Qt.MouseButton.LeftButton and self._editor_press:
            self._editor_press = False
            e.accept()
            return
        super().mouseReleaseEvent(e)
        # 拖动标点后松手: 把新位置写回方案并重画连线
        # (用户 2026-10-12 报"点之间的连线是创建后定死的, 点移动位置不能自动重新连接")
        if e.button() == Qt.MouseButton.LeftButton:
            self._sync_dragged_markers()

    def _sync_dragged_markers(self) -> bool:
        """松手后扫描所有标点: 位置变了的写回归一化坐标, 然后重画连线并落盘。

        注意必须**每个都调用** sync_marker_pos(不能用 any() —— 那会在第一个变化处短路,
        剩下的标点就不回写了), 否则一次拖动多个点时会漏。
        """
        if not self._scheme or not self._marker_items:
            return False
        changed = False
        for it in list(self._marker_items.values()):
            if it.sync_marker_pos():
                changed = True
        self._relayout_markers()
        if not changed:
            return False
        try:
            self.win.persist_schemes()           # 松手才写盘(拖动过程中别每像素一次 I/O)
        except Exception:                        # noqa: BLE001 - 落盘失败不该打断界面
            pass
        return True

    def keyPressEvent(self, e):
        if self._area_editor:
            if e.key() == Qt.Key.Key_Escape:
                self.cancel_area_edit()
                return
            if e.key() in (Qt.Key.Key_Enter, Qt.Key.Key_Return):
                self._area_editor.commit()
                return
            if e.key() == Qt.Key.Key_Backspace:
                self._area_editor.undo()
                return
        if e.key() == Qt.Key.Key_Escape and self._connect_from:
            self.cancel_connect()              # Esc 取消连线
            return
        if (e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)
                and e.modifiers() == Qt.KeyboardModifier.NoModifier):
            if not e.isAutoRepeat():
                self.delete_selected_annotations()
            e.accept()
            return
        super().keyPressEvent(e)

    def wheelEvent(self, event):
        if self._area_editor and self._area_editor.pressed is not None:
            event.accept()
            return
        if event.modifiers() & Qt.ControlModifier:
            return
        steps = event.angleDelta().y() / 120.0
        if not steps or self._base is None:
            return
        if not self._area_editor:
            self.user_adjusted_view()
        # 上下限按可视世界范围计算，4096² 与 512² 底图有一致的操作幅度。
        unit_scale = self._square_viewport() / max(self._img_w, 1.0)
        factor = self._zoom * (1.15 ** max(-20.0, min(20.0, steps)))
        factor = max(unit_scale / 4, min(unit_scale / 0.02, factor))
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        ratio = factor / self._zoom
        self.scale(ratio, ratio)
        self._zoom = factor
        self._rebuild_marker_sizes()          # 标点尺寸随缩放而变(封顶), 尺寸真变了才重建
        self.set_guard_radius(self._ratio_to_radius(self._guard_ratio))

    # ---- 底图 ----
    def set_scene_name(self, scene: str | None):
        if scene != self._scene_name:
            self.cancel_area_edit()
        self._scene_name = scene

    def load_image(self, img: QImage, *, new_scene: bool = False):
        pm = QPixmap.fromImage(img)
        first = self._base is None
        dimensions_changed = first or (pm.width(), pm.height()) != (self._img_w, self._img_h)
        trail = self.player_trail_snapshot() if dimensions_changed and not first else None
        if self._base is None:
            self._base = self._scene.addPixmap(pm)
            self._base.setZValue(0.0)
        else:
            self._base.setPixmap(pm)
        self._img_w = max(pm.width(), 1)
        self._img_h = max(pm.height(), 1)
        self._scene.setSceneRect(0, 0, self._img_w, self._img_h)
        if trail is not None:
            self.restore_player_trail(trail)
        if dimensions_changed and self._teammate_segments:
            self._redraw_teammate_trail()
        if dimensions_changed and self._teammate_deaths:
            self._redraw_teammate_deaths()
        self._refresh_fire_control_guides(self._last_objs)
        self._relayout_markers()
        for item in self._area_items.values():
            item.layout_on_map(self._img_w, self._img_h)
        if self._area_editor:
            self._area_editor.redraw()
        if self.settings.follow_player:
            if (first or new_scene) and self._area_editor is None and (
                    self._player_obj is None or self._objs_stale):
                self.fit_whole_map()
            elif dimensions_changed:
                self.refresh_display()
        elif first or (self._area_editor is None and self._fit_box is None and not self._fit_locked):
            self.fit_whole_map()
        self._rebuild_marker_sizes()          # 标点尺寸随缩放而变(封顶), 尺寸真变了才重建
        if self._fit_box is not None and self.activity_fit_enabled():
            self._apply_fit()                # 底图分辨率改变后按归一化任务范围重算

    # ---- 地图对象渲染 ----
    def last_objects(self) -> list[MapObj]:
        """当前正在地图上画的那一帧对象(供主窗口判断"还有没有东西可留")。"""
        return list(self._last_objs)

    def show_waiting_for_map(self, message: str) -> None:
        self.clear_objects()
        if self._base is not None:
            self._scene.removeItem(self._base)
            self._base = None
        self._set_banner(message)

    def clear_objects(self) -> None:
        """**彻底**清空地图对象(离线选图/换图时用)。

        注意与"空帧"的区别: 空帧(游戏在部署/阵亡界面, `/map_obj.json` 返回 0 字节)
        只是**这一帧没有数据**, 见 `set_map_objects` —— 那时必须保留上一帧, 不能把地图清空。
        """
        self._last_objs = []
        self._unit_prev.clear()
        self._unit_current.clear()
        self._teammate_positions.clear()
        self._teammate_icon_losses.reset()
        self._teammate_frame_at = None
        self._record_flight_position(None)
        self.clear_fire_control_guides()
        self._zone_sweep_timer.stop()
        self._zone_sweeps.clear()
        self._fire_control_timer.stop()
        self._fire_control_items.clear()
        self._fire_control_red = True
        self._public_layout = False
        self._objs_stale = False
        for it in self._obj_items:
            self._scene.removeItem(it)
        self._obj_items.clear()
        self._player_obj = None
        self._guard_item = None
        self.refresh_annotation_proximity()
        self._set_banner(None)

    def _set_objs_opacity(self, stale: bool) -> None:
        """没有实时数据的那一帧统一淡显(0.55), 免得把缓存当成实时情报, 并在视口内提示原因。"""
        self._objs_stale = bool(stale)
        if stale:
            self.clear_fire_control_guides()
        animate_zones = not stale and not self._public_layout and bool(self._zone_sweeps)
        for item, _, _ in self._zone_sweeps:
            item.setVisible(animate_zones)
        if not animate_zones:
            self._zone_sweep_timer.stop()
        else:
            self._animate_zone_sweeps()
            if not self._zone_sweep_timer.isActive():
                self._zone_sweep_timer.start()
        alpha = 0.55 if stale else 1.0
        for it in self._obj_items:
            try:
                it.setOpacity(0.0 if stale and it.data(0) == "teammate_heading" else alpha)
            except RuntimeError:          # C++ 侧已销毁的图元
                pass
        self.refresh_annotation_proximity()
        self._set_banner(tr(BANNER_STALE) if stale else None)

    def set_map_objects(self, objs: list[MapObj], *, record_tracks: bool = True,
                        stale: bool = False, public_layout: bool = False):
        """画这一帧地图对象。

        `objs` 为空**不等于**"地图上什么都没有"：真机实测游戏在**部署界面/阵亡等待界面**
        会对 `/map_obj.json` 返回 200 + 0 字节，poller 抛出后整帧被吞。以前这里拿到空列表
        就把 `_obj_items` 全删掉 ⇒ 占领区/出生点/我自己瞬间消失（用户 2026-10-23 报的
        「怎么不显示玩家自己了和占领区 出生点」）。现在空帧只把上一帧**淡显保留**，
        要清空得显式调 `clear_objects()`。
        """
        objs = list(objs or ())
        if not objs or stale or public_layout:
            self._record_flight_position(None)
            self._unit_prev.clear()
            self._unit_current.clear()
            self._teammate_positions.clear()
            self._teammate_icon_losses.reset()
            self._teammate_frame_at = None
        if not objs and self._last_objs:
            self._set_objs_opacity(True)      # 没有新数据: 保留上一帧, 只是淡显
            return
        if not objs:
            self._set_banner(tr(BANNER_EMPTY))    # 连上一帧都没有: 明说"在等实时帧"
            return
        self._last_objs = list(objs)          # 自适应包围盒要用它
        self._public_layout = public_layout
        # 只替换图元，不重启定时器；高频遥测刷新不能把慢闪一直重置到红色。
        self._fire_control_items.clear()
        self._zone_sweeps.clear()
        # 清除上一帧对象图元(保留标点/轨迹/事件)
        for it in self._obj_items:
            self._scene.removeItem(it)
        self._obj_items.clear()
        self._guard_item = None

        # 分类（占领点是"点+icon=capture_zone"，不是线段，也不是普通单位）
        def is_zone(o: MapObj) -> bool:
            return o.type == "capture_zone" or o.icon == "capture_zone"

        segs = [o for o in objs if o.is_segment and not is_zone(o)]
        def is_spawn(o: MapObj) -> bool:
            return o.type.startswith("respawn_base") or o.icon.startswith("respawn_base")

        spawns = [o for o in objs if not o.is_segment and is_spawn(o)]
        zones = [o for o in objs if not o.is_segment and is_zone(o)]
        targets = [o for o in objs if not o.is_segment and not is_zone(o)
                   and not is_spawn(o) and not o.is_player and target_kind(o)]
        fire_controls = [o for o in objs if is_fire_control(o)]
        units = [o for o in objs if not o.is_segment and not is_zone(o)
                 and not is_spawn(o) and not o.is_player and not target_kind(o)
                 and not is_fire_control(o)]
        if record_tracks and not stale and not public_layout:
            # 重绘界面时沿用两帧的位置，避免切换开关或缩放把位移算成零。
            self._unit_prev = self._unit_current
            self._unit_current = [(o.x, o.y, self._motion_type(o), o.side) for o in units]
        show_direction = (
            self.settings.teammate_heading and not stale and not public_layout and not self._aircraft_active
        )
        players = [o for o in objs if not o.is_segment and o.is_player]
        if record_tracks and not stale and not public_layout:
            self._record_flight_position(players[0] if players else None)
        follow = bool(players and self.settings.follow_player and not self._area_editor and not stale)
        if follow:
            self._update_follow_scale()

        # 渲染对象
        for o in segs:
            it = self._add_segment(o); self._obj_items.append(it)
        for o in spawns:
            it = self._add_spawn(o, large=public_layout and len(spawns) in (2, 4))
            self._obj_items.append(it)
        for o in zones:
            self._obj_items.extend(self._add_zone(o))
        for o in targets:
            self._obj_items.append(self._add_air_target(o))
        for o in fire_controls:
            self._obj_items.append(self._add_fire_control(o))
        if not fire_controls:
            self._fire_control_timer.stop()
            self._fire_control_red = True
        for o in units:
            ground_kind = ground_symbol_kind(o)
            show_ring = show_direction and ground_kind is not None and o.side == "friend"
            heading = self._unit_heading(o) if show_ring or not ground_kind else None
            item = self._add_unit(o, ground_kind, heading)
            self._obj_items.append(item)
            if show_ring and heading is not None:
                ring = make_heading_ring(heading)
                ring.setPos(item.pos())
                ring.setZValue(item.zValue() - 0.1)
                self._scene.addItem(ring)
                self._obj_items.append(ring)

        # 玩家
        prev_player_xy = self._last_player_xy
        if players:
            p = players[0]
            self._player_obj = p
            px, py = p.x * self._img_w, p.y * self._img_h
            self._obj_items.append(self._add_player(p))
            if record_tracks and not stale and not public_layout:
                self._update_player_track(px, py)
            self._last_player_xy = (px, py)
            # 同一个圈包含淡白填充和虚线边缘。
            if self.settings.guard_circle:
                self._guard_item = self._add_guard_circle(px, py)
                self._obj_items.append(self._guard_item)
            if follow:
                self.centerOn(px, py)
            elif not stale and not public_layout:
                self._pan_activity_view(px, py)
        else:
            # 玩家对象消失 -> 可能死亡
            if (record_tracks and not stale and not public_layout
                    and self._player_obj is not None and prev_player_xy is not None):
                st = self._tracks.get("__player__")
                if st is not None:
                    st.dead = True
                self._add_own_death_cross(*prev_player_xy)
            self._player_obj = None

        # 队友轨迹与死亡(基于友方单位)
        if record_tracks and not stale and not public_layout:
            self._update_teammate_tracks(units)
        # "缓存/上一帧"的那一帧淡显, 实时帧恢复不透明
        self._set_objs_opacity(stale)
        self._refresh_fire_control_guides(objs)

    def _update_player_track(self, px: float, py: float):
        s = self.settings
        if not s.player_trail:
            return
        st = self._tracks.setdefault("__player__", TrackState())
        if st.dead:
            # 复活: 重置轨迹
            st.dead = False
            st.last_seen = None
            st.points.clear()
            self._clear_track_items(st)
        # 坐标跳变只断开轨迹。
        if st.last_seen is not None:
            lx, ly = st.last_seen
            dpx, dpy = px - lx, py - ly
            dist = math.hypot(dpx, dpy) / self._img_w   # 归一化距离
            if dist > s.trail_jump_thresh:
                st.points.clear()
                self._clear_track_items(st)
        st.last_seen = (px, py)
        self._append_player_track(st, px, py)

    def _append_player_track(self, st: TrackState, px: float, py: float) -> None:
        if st.points:
            prev = st.points[-1]
            segment_items = []
            # 所有描边都在线芯下方，避免相邻线段的描边盖住转角。
            for color, width, layer in (("#181818", 4.0, 27.0), ("#ffffff", 2.0, 28.0)):
                pen = QPen(QColor(color), width)
                pen.setCosmetic(True)
                pen.setCapStyle(Qt.RoundCap)
                item = self._scene.addLine(prev[0], prev[1], px, py, pen)
                item.setZValue(layer)
                segment_items.append(item)
            st.seg_items.append(tuple(segment_items))
        st.points.append((px, py))
        if len(st.points) > self.settings.trail_max:
            if st.seg_items:
                for item in st.seg_items.pop(0):
                    self._scene.removeItem(item)
            st.points.pop(0)

    def player_trail_snapshot(self):
        track = self._tracks.get("__player__")
        if track is None:
            return None
        return tuple((x / self._img_w, y / self._img_h) for x, y in track.points), track.dead

    def restore_player_trail(self, snapshot) -> None:
        old = self._tracks.pop("__player__", None)
        if old is not None:
            self._clear_track_items(old)
        if snapshot is None:
            return
        points, dead = snapshot
        track = TrackState()
        for x, y in points:
            self._append_player_track(track, x * self._img_w, y * self._img_h)
        track.dead = dead
        track.last_seen = track.points[-1] if track.points else None
        for segment in track.seg_items:
            for item in segment:
                item.setVisible(self.settings.player_trail)
        self._tracks["__player__"] = track

    def _clear_track_items(self, st: TrackState):
        for segment in st.seg_items:
            for item in segment:
                self._scene.removeItem(item)
        st.seg_items.clear()

    def _add_own_death_cross(self, px: float, py: float):
        if not self.settings.own_deaths:
            return
        col = QColor("#ff2d2d")
        pen = QPen(col, 2.5)
        pen.setCapStyle(Qt.RoundCap)
        r = 7
        l1 = self._scene.addLine(px - r, py - r, px + r, py + r, pen)
        l2 = self._scene.addLine(px - r, py + r, px + r, py - r, pen)
        for item in (l1, l2):
            item.setZValue(35.0)
            item.setData(0, "own_deaths")
            item.setVisible(not self._aircraft_active)
        self._event_items.extend((l1, l2))

    def _update_teammate_tracks(self, units: list[MapObj]):
        s = self.settings
        now = monotonic()
        previous = self._teammate_positions
        if self._teammate_frame_at is None or now - self._teammate_frame_at > TEAMMATE_FRAME_GAP_SECONDS:
            previous = []
        friends = [o for o in units if o.x is not None and o.y is not None and o.side == "friend"]
        current = [(self._motion_type(o), (o.x, o.y)) for o in friends]
        self._teammate_positions = current
        self._teammate_frame_at = now
        if s.teammate_deaths:
            losses = self._teammate_icon_losses.observe([(o.type, (o.x, o.y)) for o in friends], now)
            self._teammate_death_matcher.record_losses(losses)
        else:
            self._teammate_icon_losses.reset()
            self._teammate_death_matcher.reset()
        if s.teammate_trails:
            matches = match_teammates(previous, current, min(s.trail_jump_thresh, 0.02),
                                      air_distance=s.trail_jump_thresh)
            segments = [(now, previous[old][1], current[new][1]) for old, new, continuous in matches
                        if continuous and previous[old][1] != current[new][1]]
            if segments:
                self._teammate_segments.extend(segments)
                self._redraw_teammate_trail()
                if not self._teammate_trail_timer.isActive():
                    self._teammate_trail_timer.start()

    def _redraw_teammate_trail(self):
        path = QPainterPath()
        for _, (x1, y1), (x2, y2) in self._teammate_segments:
            # 每段都从本队友的上一帧位置开始，Qt 不会连接不同线段的端点。
            path.moveTo(x1 * self._img_w, y1 * self._img_h)
            path.lineTo(x2 * self._img_w, y2 * self._img_h)
        for item in self._teammate_trail_items:
            item.setPath(path)
            item.setVisible(self.settings.teammate_trails)

    def _expire_teammate_trail(self):
        cutoff = monotonic() - TEAMMATE_TRAIL_SECONDS
        count = len(self._teammate_segments)
        while self._teammate_segments and self._teammate_segments[0][0] <= cutoff:
            self._teammate_segments.popleft()
        if len(self._teammate_segments) != count:
            self._redraw_teammate_trail()
        if not self._teammate_segments:
            self._teammate_trail_timer.stop()

    def queue_teammate_death(self, name: str, match_time: float, local_time: float) -> None:
        if self.settings.teammate_deaths:
            self._teammate_death_matcher.record_death(name, match_time, local_time)

    def resolve_teammate_deaths(self, side_of, now: float) -> None:
        if self.settings.teammate_deaths:
            positions = self._teammate_death_matcher.match(side_of, now)
            if positions:
                self._record_teammate_deaths(positions, now)

    def _record_teammate_deaths(self, positions: list[Position], now: float):
        # 同帧新增的点都是新标记，只刷新在它们之前出现且仍有效的邻居。
        self._teammate_deaths = [death for death in self._teammate_deaths if death.expires_at > now]
        for death in self._teammate_deaths:
            if any(math.dist(death.position, point) <= TEAMMATE_DEATH_RADIUS for point in positions):
                death.expires_at = now + NEARBY_DEATH_SECONDS
        self._teammate_deaths.extend(TeammateDeath(point, now + TEAMMATE_DEATH_SECONDS) for point in positions)
        self._redraw_teammate_deaths()
        self._schedule_teammate_death_expiry(now)

    def _expire_teammate_deaths(self):
        now = monotonic()
        active = [death for death in self._teammate_deaths if death.expires_at > now]
        if len(active) != len(self._teammate_deaths):
            self._teammate_deaths = active
            self._redraw_teammate_deaths()
        self._schedule_teammate_death_expiry(now)

    def _schedule_teammate_death_expiry(self, now: float):
        if self._teammate_deaths:
            deadline = min(death.expires_at for death in self._teammate_deaths)
            self._teammate_death_timer.start(max(1, math.ceil((deadline - now) * 1000)))
        else:
            self._teammate_death_timer.stop()

    def _redraw_teammate_deaths(self):
        for item in self._teammate_death_items:
            self._scene.removeItem(item)
        self._teammate_death_items.clear()

        cross_pen = QPen(QColor("#ff2d2d"), 3.2)
        cross_pen.setCapStyle(Qt.RoundCap)
        cross_outline = QPen(QColor(29, 15, 19, 245), 5.6)
        cross_outline.setCapStyle(Qt.RoundCap)
        digit_outline = QPen(cross_outline.color(), 2.3)
        digit_outline.setJoinStyle(Qt.RoundJoin)
        arm = 10.0
        path = QPainterPath(QPointF(-arm, -arm))
        path.lineTo(arm, arm)
        path.moveTo(-arm, arm)
        path.lineTo(arm, -arm)
        for group in _nearby_death_groups(self._teammate_deaths):
            count = len(group)
            item = self._scene.addPath(path, cross_pen)
            item.setFlag(QGraphicsItem.ItemIgnoresTransformations)
            item.setAcceptedMouseButtons(Qt.NoButton)
            item.setPos(sum(point[0] for point in group) * self._img_w / count,
                        sum(point[1] for point in group) * self._img_h / count)
            item.setZValue(33.5)
            item.setToolTip(tr("队友阵亡 ×{0}", count))
            item.setVisible(not self._aircraft_active and self.settings.teammate_deaths)
            outline = QGraphicsPathItem(path, item)
            outline.setPen(cross_outline)
            outline.setFlag(QGraphicsItem.ItemStacksBehindParent)
            outline.setAcceptedMouseButtons(Qt.NoButton)
            if count > 1:
                label = QGraphicsSimpleTextItem(str(count), item)
                font = _teammate_death_count_font()
                label.setFont(font)
                label.setBrush(QBrush(QColor("#f0ede4")))
                label.setAcceptedMouseButtons(Qt.NoButton)
                bounds = label.boundingRect()
                label.setPos(outline.boundingRect().left() - bounds.right() - 4, -bounds.center().y())

                edge = QGraphicsSimpleTextItem(label.text(), label)
                edge.setFont(font)
                edge.setBrush(QBrush(digit_outline.color()))
                edge.setPen(digit_outline)
                edge.setFlag(QGraphicsItem.ItemStacksBehindParent)
                edge.setAcceptedMouseButtons(Qt.NoButton)
            self._teammate_death_items.append(item)

    # ---- hud 事件: 只画"位置可证实"的环 ----
    def mark_my_death(self, label: str = tr("我被击落")) -> bool:
        """由 MainWindow 在 build_feed 判定后调用。

        只有一种击杀值得画在地图上: **我自己被击落** —— 那个位置就是我最后出现的位置, 可证实。
        别人的击杀在遥测里根本没有被击杀者坐标, 画出来就是假情报(而且是刷屏根因), 一律走事件面板。
        """
        if self._last_player_xy is None:
            return False
        px, py = self._last_player_xy
        self._add_kill_marker(px, py, enemy=True, label=label or tr("我被击落"))
        return True

    def _add_kill_marker(self, px: float, py: float, enemy: bool, label: str):
        col = QColor("#ff3b30") if enemy else QColor("#34c759")
        # 圆环 + 斜十字
        r = 10
        ring = self._scene.addEllipse(px - r, py - r, r * 2, r * 2, QPen(col, 2.0), Qt.NoBrush)
        ring.setZValue(34.0)
        pen = QPen(col, 1.8); pen.setCapStyle(Qt.RoundCap)
        c1 = self._scene.addLine(px - r * 0.6, py - r * 0.6, px + r * 0.6, py + r * 0.6, pen)
        c2 = self._scene.addLine(px - r * 0.6, py + r * 0.6, px + r * 0.6, py - r * 0.6, pen)
        c1.setZValue(34.1); c2.setZValue(34.1)
        for item in (ring, c1, c2):
            item.setData(0, "own_deaths")
            item.setVisible(not self._aircraft_active and self.settings.own_deaths)
        ring.setToolTip((tr('敌方击杀 {0}', label)) if enemy else (tr('我方击杀 {0}', label)))
        self._event_items.extend([ring, c1, c2])

    # ---- 对象图元 ----
    def _seg_points(self, o: MapObj):
        sx = (o.sx if o.sx is not None else o.x) * self._img_w
        sy = (o.sy if o.sy is not None else o.y) * self._img_h
        ex = (o.ex if o.ex is not None else (o.x or 0) + 0.03) * self._img_w
        ey = (o.ey if o.ey is not None else (o.y or 0) + 0.03) * self._img_h
        return sx, sy, ex, ey

    def _add_segment(self, o: MapObj) -> QGraphicsLineItem:
        sx, sy, ex, ey = self._seg_points(o)
        col = QColor(o.color or "#999")
        pen = QPen(col, 3.5)
        pen.setCapStyle(Qt.RoundCap)
        it = QGraphicsLineItem(sx, sy, ex, ey)
        it.setPen(pen); it.setZValue(5.0)
        self._scene.addItem(it)
        for cx, cy in ((sx, sy), (ex, ey)):
            dot = QGraphicsEllipseItem(cx - 3, cy - 3, 6, 6)
            dot.setBrush(QBrush(col)); dot.setPen(QPen(col))
            dot.setZValue(5.1)
            self._scene.addItem(dot); self._obj_items.append(dot)
        name = tr(OBJ_TYPES.get(o.type, ("对象", ""))[0])
        it.setToolTip(f"[{name}] {o.color}")
        return it

    def _add_spawn(self, o: MapObj, *, large: bool = False):
        if o.x is None:
            return self._add_segment(o)
        px, py = o.x * self._img_w, o.y * self._img_h
        it = make_spawn_item(o.color or "#999999", large=large)
        it.setPos(px, py)
        it.setZValue(8.0)
        name = tr(OBJ_TYPES.get(o.type, ("出生点", ""))[0])
        it.setToolTip(f"[{name}] {o.color}")
        self._scene.addItem(it)
        return it

    def _blink_fire_control(self) -> None:
        self._fire_control_red = not self._fire_control_red
        for item in self._fire_control_items:
            item.set_red(self._fire_control_red)

    def clear_fire_control_guides(self) -> None:
        for item in self._fire_control_guides:
            self._scene.removeItem(item)
        self._fire_control_guides.clear()

    def _record_flight_position(self, player: MapObj | None) -> None:
        # 只用连续实时坐标的位移，避免把机头朝向或界面重绘当成航迹。
        previous = self._flight_position
        self._flight_delta = self._flight_position = None
        if player is None or player.type != "aircraft" or player.x is None or player.y is None:
            return
        self._flight_position = (player.x, player.y)
        if previous is not None:
            dx, dy = player.x - previous[0], player.y - previous[1]
            if math.hypot(dx, dy) > 1e-6:
                self._flight_delta = (dx, dy)

    def _refresh_fire_control_guides(self, objs: list[MapObj]) -> None:
        self.clear_fire_control_guides()
        if (self._base is None or self._objs_stale or self._public_layout
                or getattr(self.win, "_map_locked", False)):
            return
        player = next((o for o in objs if o.is_player and not o.is_segment), None)
        fire_controls = [o for o in objs if is_fire_control(o)]
        if player is None or not fire_controls:
            return
        runways = [o for o in objs if o.type == "airfield"]
        width, height = self._img_w, self._img_h
        position = (player.x * width, player.y * height)
        movement = None
        if self._flight_delta is not None:
            dx, dy = self._flight_delta
            movement = (dx * width, dy * height)
        for marker in fire_controls:
            target = (marker.x * width, marker.y * height)
            approach = runway_approach(position, target, runways, width, height)
            start, end = approach if approach is not None else (position, target)
            if start == end:
                continue
            if approach is not None:
                color = "#63e4ef"
            else:
                aligned = movement is not None and course_aligned(movement, position, target)
                color = "#63e68a" if aligned else "#ff9898"
            pen = QPen(QColor(color), 1.5)
            pen.setCosmetic(True)
            pen.setStyle(Qt.DashLine if approach else Qt.SolidLine)
            line = self._scene.addLine(*start, *end, pen)
            line.setZValue(6.0)
            line.setAcceptedMouseButtons(Qt.NoButton)
            self._fire_control_guides.append(line)

    def _add_fire_control(self, o: MapObj) -> FireControlMarker:
        item = FireControlMarker(self._fire_control_red)
        item.setPos(o.x * self._img_w, o.y * self._img_h)
        item.setZValue(44.0)
        item.setToolTip(tr('[火控指定点 SPI] ({0:.3f},{1:.3f})', o.x, o.y))
        self._scene.addItem(item)
        self._fire_control_items.append(item)
        if not self._fire_control_timer.isActive():
            self._fire_control_timer.start()
        return item

    def _add_air_target(self, o: MapObj):
        kind = target_kind(o)
        # 明确的轰炸/防守类型决定敌我语义；碉堡保留数据源的阵营颜色。
        color = {"bombing_point": TEAM_COLORS["enemy"], "defending_point": TEAM_COLORS["friend"]}.get(
            kind, o.color or "#999999")
        item = make_target_item(kind, color)
        item.setPos(o.x * self._img_w, o.y * self._img_h)
        item.setZValue(12.0)  # 碉堡周边火炮/防空单位常重合，固定目标保持在普通载具之上。
        name = {"bombing_point": tr("敌方轰炸战区"), "defending_point": tr("我方防守战区"), "pillbox": tr("碉堡")}[kind]
        side = {"enemy": tr("敌方"), "friend": tr("友方"), "player": tr("我方"), "neutral": tr("中立")}[o.side]
        item.setToolTip(f"[{name}] {side} ({o.x:.3f},{o.y:.3f})")
        self._scene.addItem(item)
        return item

    @staticmethod
    def _motion_type(obj: MapObj) -> str:
        return f"{obj.type}/{obj.icon.casefold()}" if obj.type == "ground_model" else obj.type

    def _unit_heading(self, o: MapObj) -> float | None:
        """朝向 = 路径切线。

        优先 `/map_obj` 的 `dx,dy`（真机 184 个对象里只有 3 个非零，地面单位恒为 0），
        否则用**逐帧最近邻配对**的位移（以前按坐标当 key 找轨迹，坐标一变 key 就变，
        所以永远拿不到切线 —— 图元就"一直指向北"）。都没有就不定向，绝不默认朝北。
        """
        h = shapes.heading_deg(o.dx, o.dy)
        if h is not None:
            return h
        if o.x is None:
            return None
        return shapes.motion_deg((o.x, o.y), self._unit_prev, self._motion_type(o), o.side)

    def _add_unit(self, o: MapObj, ground_kind: str | None, heading: float | None) -> QGraphicsItem:
        px, py = o.x * self._img_w, o.y * self._img_h
        if ground_kind:
            item = make_target_item(ground_kind, o.color or "#999999")
            item.setPos(px, py)
            item.setZValue(10.0)
            name = {"artillery": tr("火炮"), "tank_destroyer": tr("坦克歼击车"), "spaa": tr("防空单位"),
                    "sam": tr("防空导弹"), "light_tank": tr("轻型坦克"), "medium_tank": tr("中型坦克"),
                    "heavy_tank": tr("重型坦克"), "ifv": tr("步兵战车"), "wheeled": tr("轮式单位"),
                    "ground_unknown": tr('地面单位 · {0}', o.icon)}[ground_kind]
            side = {"enemy": tr("敌方"), "friend": tr("友方"), "player": tr("我方"), "neutral": tr("中立")}[o.side]
            item.setToolTip(f"[{name}] {side} ({o.x:.3f},{o.y:.3f})")
            self._scene.addItem(item)
            return item
        col = QColor(o.color or "#999")
        length = self.px_to_scene(shapes.unit_len(o.side))     # 屏幕像素恒定
        if o.side == "friend":
            tri = QPolygonF([QPointF(x, y) for x, y in shapes.arrow_dart(length)])
        elif o.type == "aircraft":
            h2 = length / 2.0
            tri = QPolygonF([QPointF(0, -h2), QPointF(h2 * 0.75, h2), QPointF(-h2 * 0.75, h2)])
        else:
            h2 = length / 2.0
            tri = QPolygonF([QPointF(0, -h2), QPointF(h2, 0), QPointF(0, h2), QPointF(-h2, 0)])
        it = QGraphicsPolygonItem(tri)
        it.setBrush(QBrush(col))
        it.setPen(QPen(QColor(0, 0, 0, 160), self.px_to_scene(1.2)))   # 描边也按屏幕像素
        it.setPos(px, py); it.setZValue(10.0)
        if heading is not None:
            it.setRotation(heading)
        name = tr(OBJ_TYPES.get(o.type, ("单位", ""))[0])
        side = {"enemy": tr("敌方"), "friend": tr("友方"), "player": tr("我方"), "neutral": ""}[o.side]
        it.setToolTip(f"[{name}]{side} {o.color} ({o.x:.3f},{o.y:.3f})")
        self._scene.addItem(it)
        return it

    def _add_guard_circle(self, px: float, py: float) -> QGraphicsEllipseItem:
        """以我为中心的警戒圈，半径与「附近队友阵亡」告警判定一致。

        半径是归一化坐标(0.10 ≈ 地图宽度 10%), x/y 分别乘图幅, 保证和 threatwatch 的
        hypot 判定看到的是同一个圈。"""
        rx = self._guard_radius * self._img_w
        ry = self._guard_radius * self._img_h
        pen = QPen(QColor("#ffd60a"), 2.0, Qt.DashLine)
        pen.setCosmetic(True)
        it = QGraphicsEllipseItem(px - rx, py - ry, rx * 2, ry * 2)
        it.setPen(pen)
        it.setBrush(QBrush(QColor(255, 255, 255, 50)))
        it.setZValue(8.5)                          # 单位(10)/占领区(9.5)之下, 不糊住图标
        it.setToolTip(tr('警戒圈: 队友连死告警半径 {0:.2f}(归一化)', self._guard_radius))
        self._scene.addItem(it)
        return it

    def _add_zone(self, o: MapObj) -> list:
        """实心菱形表示已归属，争夺中在菱形内转动扇区。"""
        px, py = o.x * self._img_w, o.y * self._img_h
        d = self.px_to_scene(shapes.zone_diameter())
        r = d / 2.0
        items: list = []
        team_colors = {**TEAM_COLORS, "player": TEAM_COLORS["friend"]}
        owner = o.zone_owner
        fill_hex = team_colors.get(owner, "#ffffff")
        outline = {"friend": "#b9caff", "enemy": "#ffc1bb"}.get(owner, "#101010")
        status = tr({"friend": "我方已占领", "enemy": "敌方已占领"}.get(owner, "未占领"))
        if o.zone_contested:
            fill_hex = "#24303c"
            outline = "#eef2f6"
            status = tr("争夺中 · 进度未知")
        tip = tr('[占领点 {0}] {1}', o.zone_label or o.zone_id, status)
        diamond = QGraphicsPolygonItem(QPolygonF([
            QPointF(px, py - r), QPointF(px + r, py),
            QPointF(px, py + r), QPointF(px - r, py)]))
        diamond.setBrush(QBrush(QColor(fill_hex)))
        neutral = owner == "neutral" and not o.zone_contested
        stroke_width = max(1.5, self.px_to_scene(2.0)) if neutral else self.px_to_scene(1.5)
        pen = QPen(QColor(outline), stroke_width)
        if o.zone_contested:
            pen.setDashPattern([2.8, 1.6])
        diamond.setPen(pen)
        diamond.setZValue(9.5)
        diamond.setToolTip(tip)
        self._scene.addItem(diamond); items.append(diamond)
        if o.zone_contested:
            inner = r - self.px_to_scene(2.5)
            clip = QPainterPath(QPointF(px, py - inner))
            clip.lineTo(px + inner, py)
            clip.lineTo(px, py + inner)
            clip.lineTo(px - inner, py)
            clip.closeSubpath()
            sweep = QGraphicsPathItem()
            sweep.setBrush(QColor(team_colors.get(o.side, "#d9e3ee")))
            sweep.setPen(QPen(Qt.PenStyle.NoPen))
            sweep.setZValue(9.55)
            sweep.setToolTip(tip)
            bounds = QRectF(px - r, py - r, d, d)
            self._zone_sweeps.append((sweep, clip, bounds))
            self._scene.addItem(sweep)
            items.append(sweep)
        label = o.zone_label or (chr(ord("A") + o.zone_id) if o.zone_id >= 0 else "")
        if label:
            txt = QGraphicsSimpleTextItem(label)
            f = txt.font()
            # 字号也必须按屏幕像素折算: 直接给"场景字号"会让屏幕字号 = 字号 × zoom,
            # 换图 / 手动缩放时 A B C 忽大忽小(用户实测就是这个)。下限只能是 1 像素 ——
            # 给大了(曾用 max(6,…))在放大地图时反而把字号顶上去, 屏幕字号又漂了。
            f.setPixelSize(max(1, int(round(self.px_to_scene(shapes.ZONE_LABEL_PX)))))
            f.setBold(True)
            txt.setFont(f)
            txt.setBrush(QBrush(QColor("#101010" if neutral else "#ffffff")))
            txt.setZValue(9.6)
            txt.setToolTip(tip)
            br = txt.boundingRect()
            txt.setPos(px - br.width() / 2.0, py - br.height() / 2.0)
            self._scene.addItem(txt); items.append(txt)
        return items

    def _animate_zone_sweeps(self) -> None:
        # 8111 只提供 blink。固定宽度的扇区表示正在争夺，不随时间填满。
        # Qt 的正角度是逆时针，单调时钟让重绘后的扇区延续同一相位。
        angle = 90.0 + (monotonic() % 1.8) * 200.0
        for item, clip, bounds in self._zone_sweeps:
            sector = QPainterPath(bounds.center())
            sector.arcTo(bounds, angle, 90.0)
            sector.closeSubpath()
            item.setPath(clip.intersected(sector))

    def _add_player(self, o: MapObj) -> QGraphicsPolygonItem:
        px, py = o.x * self._img_w, o.y * self._img_h
        size = self.px_to_scene(shapes.player_len())     # 屏幕尺寸恒定
        tri = QPolygonF([QPointF(0, -size / 2), QPointF(size * 0.30, size / 2), QPointF(-size * 0.30, size / 2)])
        it = QGraphicsPolygonItem(tri)
        it.setBrush(QBrush(QColor("#ffffff")))
        it.setPen(QPen(QColor("#111111"), self.px_to_scene(2.0)))
        it.setPos(px, py); it.setZValue(PLAYER_LAYER)
        heading = shapes.heading_deg(o.dx, o.dy)
        if heading is None and self._last_player_xy is not None:
            heading = shapes.tangent_deg(self._last_player_xy, (px, py))
        if heading is not None:
            it.setRotation(heading)
        it.setToolTip(tr('[玩家] {0} ({1:.3f},{2:.3f})', o.color, o.x, o.y))
        self._scene.addItem(it)
        return it

    # ---- 清理 ----
    def clear_trail(self):
        for st in self._tracks.values():
            self._clear_track_items(st)
        self._teammate_positions.clear()
        self._teammate_icon_losses.reset()
        self._teammate_death_matcher.reset()
        self._teammate_frame_at = None
        self._teammate_segments.clear()
        self._teammate_trail_timer.stop()
        self._redraw_teammate_trail()
        self._teammate_deaths.clear()
        self._teammate_death_timer.stop()
        self._redraw_teammate_deaths()
        # 清事件标记(死亡/击杀)
        for it in self._event_items:
            self._scene.removeItem(it)
        self._event_items.clear()
        self._tracks.clear()

    def on_reconnect(self):
        """重连或场景切换时清除轨迹与事件标记, 避重生直线。"""
        self.clear_trail()
        self._last_player_xy = None
        # 换图/重连: 上一张图的对象不能留在新图上(新图的位置全变了), 等下一帧重新画
        self.clear_objects()

    # ---- 标点布局 ----
    def start_selection_blink(self):
        if not self._selection_timer.isActive():
            self._selection_dim = True
            self._selection_timer.start()

    def _blink_selected_markers(self):
        selected = [item for item in self._scene.selectedItems() if isinstance(item, MarkerItem)]
        if not selected:
            self._selection_timer.stop()
            return
        self._selection_dim = not self._selection_dim
        for item in selected:
            item.update()

    def _relayout_markers(self):
        for m_item in self._marker_items.values():
            x = m_item.marker.cx * self._img_w
            y = m_item.marker.cy * self._img_h
            s = m_item.scale()                    # 图元带 scale, 居中偏移要跟着缩放
            m_item.setPos(QPointF(x, y) - m_item.boundingRect().center() * s)
        self._redraw_paths()

    def make_marker_pixmap(self, type_: str, *, outline: bool = False) -> QPixmap:
        """地图标点图标(**固定高清源**, 绘制见 marker_icons.py)。

        尺寸**不靠重建 pixmap**, 而是"高清源 + 图元 scale":
          * pixmap 一律画 64px(够清楚);
          * 屏幕尺寸 = clamp(50 × zoom, 14, 24) px —— 用户 2026-10-12 三次修订的结论
            ("放大地图编辑后有些挡屏幕" → "还是过大了"): fit 视图本来约 23px, 所以**封顶就取
            24px**, 放大编辑时标点**不再变大**, 只有缩小地图时才跟着变小; 下限 14px 保证看得见;
          * 由 `_apply_marker_scale()` 把 `目标屏幕 ÷ zoom ÷ 64` 设为图元 scale。
        为什么不用"重建不同尺寸的 pixmap": pixmap 边长只能取整, ×6 时目标场景尺寸仅 4px,
        取整/下限会让屏幕尺寸严重跑偏(实测飙到 54px); scale 是浮点变换, 精确且不用删建图元。
        """
        from .marker_icons import marker_pixmap
        return marker_pixmap(type_, 64, outline=outline)

    def refresh_annotation_proximity(self) -> None:
        player = self._player_obj
        if (player is None or self._objs_stale or self._public_layout
                or getattr(self.win, "_map_locked", False)):
            for item in self._marker_items.values():
                item.set_fill_opacity(1.0)
            for item in self._path_items:
                item.set_fill_opacity(1.0)
            self._area_boundaries.show_boundaries(QRectF(), ())
            return

        x, y = player.x * self._img_w, player.y * self._img_h
        pos = QPointF(x, y)
        inner = self.px_to_scene(ANNOTATION_OUTLINE_PX)
        outer = self.px_to_scene(ANNOTATION_FADE_PX)
        for item in self._marker_items.values():
            dx = x - item.marker.cx * self._img_w
            dy = y - item.marker.cy * self._img_h
            item.set_fill_opacity(_annotation_opacity(math.hypot(dx, dy), inner, outer))
        for item in self._path_items:
            distance = item.distance_to(pos, max_distance=outer)
            item.set_fill_opacity(_annotation_opacity(distance, inner, outer))

        radius = self.px_to_scene(shapes.player_len() / 2 + 3.0)
        rect = QRectF(x - radius, y - radius, 2 * radius, 2 * radius)
        self._area_boundaries.show_boundaries(rect, (item for item in self._area_items.values()
                                                     if item.boundary_near(rect)))

    def _marker_screen_px(self) -> float:
        """标点当前应有的**屏幕**像素尺寸: 随缩放, 14~24px 封底封顶。"""
        z = float(getattr(self, "_zoom", 1.0) or 1.0)
        return max(14.0, min(24.0, 50.0 * z))

    def _apply_marker_scale(self) -> None:
        """按当前缩放给每个标点设图元 scale(= 目标屏幕尺寸 ÷ zoom ÷ 源像素)。"""
        if not self._marker_items:
            return
        z = max(float(getattr(self, "_zoom", 1.0) or 1.0), 1e-6)
        s = self._marker_screen_px() / z / 64.0
        # 不做"值没变就跳过": 图元可能刚被 _rebuild_markers 重建过(那时 scale 还是 1),
        # 一跳过就会让新点以原始 64px 显示(实测过这个坑)。setScale 很便宜, 每次设就是了。
        for it in self._marker_items.values():
            it.setScale(s)
        self._relayout_markers()

    def _rebuild_marker_sizes(self) -> None:
        """缩放流程的统一入口(名字保持, 调用点都在 _zoom 变化处)。"""
        self._apply_marker_scale()
        self._draw_references()

    # ---- 方案 ----
    def set_scheme(self, scheme: Scheme | None):
        self.cancel_area_edit()
        self.cancel_connect()
        self._select_origin = None
        self._band.hide()
        self._quick_press = None
        self._editor_press = False
        last_type = scheme.markers[-1].type if scheme and scheme.markers else None
        if last_type == "intense":
            last_type = "engage"
        self._last_marker_type = last_type if last_type in MARKER_TYPES and last_type not in HIDDEN_TYPES else None
        self._scene.clearSelection()
        self._scheme = scheme
        self._rebuild_markers()
        self._rebuild_areas()
        self.interaction_changed.emit()

    def _rebuild_markers(self):
        selected = {it.marker.id for it in self._scene.selectedItems() if isinstance(it, MarkerItem)}
        for it in list(self._marker_items.values()):
            self._scene.removeItem(it)
        self._marker_items.clear()
        for pit in self._path_items:
            self._scene.removeItem(pit)
        self._path_items.clear()
        if not self._scheme:
            self._relayout_markers()
            return
        for m in self._scheme.markers:
            it = MarkerItem(m, self)
            self._scene.addItem(it)
            self._marker_items[m.id] = it
            it.setSelected(m.id in selected)
        self._apply_marker_scale()        # 新图元的 scale 默认是 1, 先套上当前尺寸再定位
        self._relayout_markers()

    def current_scheme(self) -> Scheme | None:
        return self._scheme

    def _rebuild_areas(self):
        for item in self._area_items.values():
            self._scene.removeItem(item)
        self._area_items.clear()
        if self._scheme:
            for area in self._scheme.areas:
                item = AreaItem(area, self._img_w, self._img_h, selectable=True)
                self._scene.addItem(item)
                self._area_items[area.id] = item
        self.refresh_annotation_proximity()

    def area_creation_menu(self, parent):
        menu = QMenu(tr("绘制区域"), parent)
        menu.setEnabled(self._scheme is not None and self._base is not None)
        for kind, label in AREA_TYPES.items():
            shapes_menu = menu.addMenu(tr(label))
            for mode, shape_label in AREA_DRAW_MODES:
                shapes_menu.addAction(tr(shape_label), lambda k=kind, m=mode: self.start_area_edit(k, m))
        return menu

    def start_area_edit(self, kind, mode, existing=None):
        if self._scheme is None or self._base is None:
            return
        if existing is not None and not any(area is existing for area in self._scheme.areas):
            return
        if existing is None and len(self._scheme.areas) >= MAX_AREAS:
            self._hint(tr('每套方案最多保存 {0} 个区域', MAX_AREAS), 4000)
            return
        self.end_editing()
        self._quick_press = None
        self._drag_anchor = None
        self._area_editor = AreaEditor(self, kind, mode, existing)
        self.interaction_changed.emit()

    def cancel_area_edit(self):
        if self._area_editor:
            editor, self._area_editor = self._area_editor, None
            editor.close()
            self._hint(tr("已取消区域绘制"), 3000)
            self.interaction_changed.emit()

    def add_area_actions(self, menu, area):
        types = menu.addMenu(tr("更改区域类型"))
        for kind, label in AREA_TYPES.items():
            action = types.addAction(tr(label), lambda k=kind: self._change_area_type(area, k))
            action.setEnabled(kind != area.type)
        shapes_menu = menu.addMenu(tr("重画边界"))
        for mode, label in AREA_DRAW_MODES:
            shapes_menu.addAction(tr(label), lambda m=mode: self.start_area_edit(area.type, m, area))
        menu.addAction(tr("删除区域"), lambda: self._save_area_change(area, None))

    def _change_area_type(self, area, kind):
        self._save_area_change(area, Area.new(kind, area.shape, area.geometry, area_id=area.id))

    def _save_area_change(self, old, new):
        scheme = self._scheme
        if scheme is None:
            return False
        previous = scheme.areas
        updated = previous.copy()
        if old is None:
            updated.append(new)
        else:
            index = next((i for i, area in enumerate(previous) if area is old), None)
            if index is None:
                return False
            updated[index:index + 1] = [new] if new is not None else []
        scheme.areas = updated
        if not self.win.persist_schemes():
            scheme.areas = previous
            return False
        if self._scheme is scheme:
            self._rebuild_areas()
            if new is not None:
                self._scene.clearSelection()
                self._area_items[new.id].setSelected(True)
        return True

    def _redraw_paths(self):
        selected = {id(item._link) for item in self._path_items if item.isSelected()}
        for pit in self._path_items:
            self._scene.removeItem(pit)
        self._path_items.clear()
        if not self._scheme:
            return
        for pl, reverse in visible_link_pairs(self._scheme.paths):
            a = self._marker_items.get(pl.from_id)
            b = self._marker_items.get(pl.to_id)
            if not a or not b:
                continue
            line = SchemePathItem(pl, a.sceneBoundingRect().center(), b.sceneBoundingRect().center(),
                                  a.marker.type, b.marker.type, reverse)
            line.layout(self._zoom, self._marker_screen_px() / 2)
            line.setZValue(15.0)
            line.setFlag(QGraphicsItem.ItemIsSelectable, True)
            self._scene.addItem(line)
            self._path_items.append(line)
            line.setSelected(id(pl) in selected or (reverse is not None and id(reverse) in selected))
        self.refresh_annotation_proximity()

    # ---- 右键交互 ----
    def _ctx(self, pos):
        if self._area_editor:
            return
        menu = self._marker_menu(pos)
        # A shortcut can reach the focused map while the pointer is over the popup.
        self._menu_position = pos
        try:
            menu.exec(self.viewport().mapToGlobal(pos))
        finally:
            self._menu_position = None
            menu.deleteLater()

    def _editable_annotation(self, it):
        return isinstance(it, (MarkerItem, AreaItem)) and it.parentItem() is None

    def _annotation_at(self, pos, annotations):
        for item in annotations:
            if isinstance(item, MarkerItem):
                return item
        point = self.mapToScene(pos)
        distance, line = min(((item.distance_to(point), item) for item in reversed(self._path_items)),
                             default=(math.inf, None), key=lambda hit: hit[0])
        if distance * self.transform().m11() <= SCHEME_LINK_HIT_RADIUS_PX:
            return line
        return annotations[0] if annotations else None

    def selected_annotations(self):
        if self._scheme is None:
            return None
        selected = self._scene.selectedItems()
        return scheme_edit.selection(self._scheme, {i.marker.id for i in selected if isinstance(i, MarkerItem)},
                                     {i.area.id for i in selected if isinstance(i, AreaItem)})

    def delete_selected_annotations(self):
        scheme = self._scheme
        if scheme is None or self._area_editor or self._connect_from:
            return
        selection = self._scene.selectedItems()
        markers = {item.marker.id for item in selection if isinstance(item, MarkerItem)}
        areas = {item.area.id for item in selection if isinstance(item, AreaItem)}
        links = set()
        for item in selection:
            if isinstance(item, SchemePathItem):
                links.add(id(item._link))
                if item.reverse_item is not None:
                    links.add(id(item.reverse_item._link))
        if not (markers or areas or links):
            return

        previous = scheme.markers, scheme.areas, scheme.paths
        scheme.markers = [marker for marker in scheme.markers if marker.id not in markers]
        scheme.areas = [area for area in scheme.areas if area.id not in areas]
        scheme.paths = [link for link in scheme.paths if id(link) not in links
                        and link.from_id not in markers and link.to_id not in markers]
        if not self.win.persist_schemes():
            scheme.markers, scheme.areas, scheme.paths = previous
            return
        if markers:
            self._rebuild_markers()
        elif links:
            self._redraw_paths()
        if areas:
            self._rebuild_areas()

    def copy_annotations(self):
        group = self.selected_annotations()
        if group and (group.markers or group.areas):
            self._clipboard = group

    def _can_paste_annotations(self):
        return self._scheme is not None and self._clipboard is not None and self._clipboard.scene == self._scheme.scene

    def _annotation_destination(self, pos):
        if pos is None:
            pos = self._menu_position
        if pos is None:
            pos = self.viewport().mapFromGlobal(QCursor.pos())
        if self.viewport().rect().contains(pos):
            point = self.mapToScene(pos)
            if 0 <= point.x() <= self._img_w and 0 <= point.y() <= self._img_h:
                return point.x() / self._img_w, point.y() / self._img_h
        self._hint(tr("请将鼠标移到地图上的目标位置"), 3000)
        return None

    def paste_annotations(self, pos=None, *, in_place=False):
        if self._area_editor or not self._can_paste_annotations():
            return
        position = None if in_place else self._annotation_destination(pos)
        if in_place or position is not None:
            self.apply_group(self._scheme, self._clipboard, position)

    def move_selected_annotations(self, pos=None):
        if self._area_editor:
            return
        group = self.selected_annotations()
        if group and (group.markers or group.areas):
            position = self._annotation_destination(pos)
            if position is not None:
                self.apply_group(self._scheme, group, position, move=True)

    def apply_group(self, target, group, position=None, *, move=False):
        previous = target.markers[:], target.areas[:], target.paths[:]
        try:
            markers, areas = scheme_edit.transfer(target, group, position, move=move)
        except ValueError as exc:
            QMessageBox.warning(self, tr("标注编辑"), str(exc))
            return
        if self.win.persist_schemes() is False:
            target.markers, target.areas, target.paths = previous
            return
        if target is self._scheme:
            self.set_scheme(target)
            with QSignalBlocker(self._scene):
                for key in markers:
                    self._marker_items[key].setSelected(True)
                for key in areas:
                    self._area_items[key].setSelected(True)
            self._scene.selectionChanged.emit()

    def set_references(self, schemes):
        self._references = schemes
        self._draw_references()

    def _draw_references(self):
        if self._reference_group is not None:
            self._scene.removeItem(self._reference_group)
            self._reference_group = None
        if not self._references:
            return
        group = QGraphicsItemGroup()
        group.setOpacity(.55)
        group.setZValue(.8)
        group.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self._scene.addItem(group)
        self._reference_group = group
        scale = self._marker_screen_px() / max(self._zoom, 1e-6) / 64
        for scheme in self._references:
            points = {m.id: QPointF(m.cx * self._img_w, m.cy * self._img_h) for m in scheme.markers}
            types = {m.id: m.type for m in scheme.markers}
            for area in scheme.areas:
                AreaItem(area, self._img_w, self._img_h).setParentItem(group)
            for link, reverse in visible_link_pairs(scheme.paths):
                a, b = link.from_id, link.to_id
                if a in points and b in points:
                    line = SchemePathItem(link, points[a], points[b], types[a], types[b], reverse)
                    line.setParentItem(group)
                    line.setZValue(2)
                    line.layout(self._zoom, self._marker_screen_px() / 2)
            for m in scheme.markers:
                item = QGraphicsPixmapItem(self.make_marker_pixmap(m.type), group)
                item.setOffset(-32, -32)
                item.setScale(scale)
                item.setPos(points[m.id])
                item.setZValue(3)

    def _marker_menu(self, pos) -> QMenu:
        annotations = [it for it in self.items(pos) if self._editable_annotation(it)]
        item = self._annotation_at(pos, annotations)
        gpos = self.mapToScene(pos)
        menu = QMenu(self)
        hits = [hit for hit in annotations if isinstance(hit, AreaItem)]
        selected = self.selected_annotations()
        if isinstance(item, MarkerItem) and not item.isSelected() and not (selected.markers or selected.areas):
            item.setSelected(True)
            selected = self.selected_annotations()
        link = getattr(item, "_link", None) if item is not None else None
        if link is not None:
            # 右键点**在连线上** → 直接给"删除这条连线"(用户 2026-10-13: "选错了删不掉")
            def _nm(mid: str) -> str:
                it = self._marker_items.get(mid)
                if it is None:
                    return "?"
                m = it.marker
                return m.label or tr(MARKER_TYPES.get(m.type, ("自定义", "", ""))[0])
            _del = QAction(tr('删除这条连线（{0} → {1}）', _nm(link.from_id), _nm(link.to_id)), menu)
            _del.triggered.connect(lambda: self._delete_path(link))
            menu.addAction(_del)
            reverse_item = getattr(item, "reverse_item", None)
            if reverse_item is not None:
                reverse = reverse_item._link
                menu.addAction(tr('删除这条连线（{0} → {1}）', _nm(reverse.from_id), _nm(reverse.to_id)),
                               lambda: self._delete_path(reverse))
                menu.addAction(tr("删除双向连线"), lambda: self._delete_path(link, reverse))
            menu.addSeparator()
        if isinstance(item, MarkerItem):
            _edit = QAction(tr("编辑标点..."), menu); _edit.triggered.connect(lambda: self._edit_clicked_marker(item))
            menu.addAction(_edit)
            _del = QAction(tr("删除标点"), menu); _del.triggered.connect(lambda: self._delete_markeritem(item))
            menu.addAction(_del)
            # 连线改成**点选目标点**(原来弹一串"点1 (a1b2c3)"根本认不出谁是谁)
            _conn = QAction(tr("从这里连线 → 点选目标点"), menu)
            _conn.triggered.connect(lambda: self.start_connect(item))
            menu.addAction(_conn)
            _conn2 = QAction(tr("从这里连线（用列表选…）"), menu)
            _conn2.triggered.connect(lambda: self._start_connect(item))
            menu.addAction(_conn2)
        else:
            add = menu.addMenu(tr("添加标点"))
            for key, name in MARKER_TYPE_CHOICES:
                act = QAction(tr(name), add)
                act.triggered.connect(lambda _=False, k=key: self._add_marker_at(gpos, k))
                add.addAction(act)
        menu.addMenu(self.area_creation_menu(menu))
        for hit in hits:
            number = self._scheme.areas.index(hit.area) + 1
            self.add_area_actions(menu.addMenu(f"{tr(AREA_TYPES[hit.area.type])} {number}"), hit.area)
        self._add_clipboard_actions(menu, selected, pos)
        return menu

    def _add_clipboard_actions(self, menu, selected, pos):
        has_selection = selected and (selected.markers or selected.areas)
        can_paste = self._can_paste_annotations()
        if not (has_selection or can_paste):
            return

        def add_shortcut_action(text, callback, shortcut):
            action = menu.addAction(text, callback)
            action.setShortcut(QKeySequence(shortcut))
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.setShortcutVisibleInContextMenu(True)

        menu.addSeparator()
        if has_selection:
            add_shortcut_action(tr("复制"), self.copy_annotations, "Ctrl+C")
        if can_paste:
            add_shortcut_action(tr("原位粘贴"), lambda: self.paste_annotations(in_place=True), "Ctrl+Shift+V")
            add_shortcut_action(tr("粘贴到此"), lambda: self.paste_annotations(pos), "Ctrl+V")
        if has_selection:
            add_shortcut_action(tr("移到此"), lambda: self.move_selected_annotations(pos), "Ctrl+X")

    def _refresh_death_marker_visibility(self):
        if not self.settings.teammate_deaths:
            self._teammate_icon_losses.reset()
            self._teammate_death_matcher.reset()
        visible = not self._aircraft_active
        for item in self._teammate_death_items:
            item.setVisible(visible and self.settings.teammate_deaths)
        for item in self._event_items:
            item.setVisible(visible and self.settings.own_deaths)

    def refresh_display(self):
        """开关改变后立即重绘，不依赖后续游戏帧。"""
        for track in self._tracks.values():
            for segment in track.seg_items:
                for item in segment:
                    item.setVisible(self.settings.player_trail)
        for item in self._teammate_trail_items:
            item.setVisible(self.settings.teammate_trails)
        self._refresh_death_marker_visibility()
        if self._last_objs:
            self.set_map_objects(self._last_objs, record_tracks=False, stale=self._objs_stale,
                                 public_layout=self._public_layout)

    def overlay_items(self) -> set[str]:
        """当前开着的叠加项(主窗口拿它持久化)。"""
        return {str(x) for x in self.overlay.items}

    def set_overlay_items(self, keys) -> None:
        """按存下来的开关恢复叠加项(认不出的键直接丢掉, 不炸)。"""
        self.overlay.items = {str(k) for k in (keys or ()) if str(k) in ITEM_LABELS}
        self.overlay.update()

    def _toggle_overlay(self, item: str) -> None:
        self.overlay.toggle(item)
        self.overlay.update()
        self.overlay_items_changed.emit()      # 主窗口收到后写进 ui_state.json

    def _add_marker_at(self, scene_pos, type_: str):
        if not self._scheme:
            QMessageBox.information(self, tr("提示"), tr("请先在右侧「标点方案」面板中新建方案。"))
            return
        cx = max(0.0, min(1.0, scene_pos.x() / self._img_w))
        cy = max(0.0, min(1.0, scene_pos.y() / self._img_h))
        name, _, _ = MARKER_TYPES.get(type_, (tr("自定义"), "#888", "dot"))
        label, ok = QInputDialog.getText(self, tr("标点标签"), tr('为 {0} 设置标签(可留空):', tr(name)))
        if not ok:
            return
        self._scheme.markers.append(Marker.new(type_, cx, cy, label))
        self._last_marker_type = type_
        self._rebuild_markers()
        self.win.persist_schemes()

    # ---- 快捷标点 ----
    def _hint(self, text: str, timeout: int = 0) -> None:
        win = getattr(self, "win", None)
        if win is not None and hasattr(win, "statusBar"):
            win.statusBar().showMessage(text, timeout)

    def quick_marker(self, viewport_pos) -> None:
        """Shift 点击已有节点连接；点空白沿用上一标点类型，不弹标签框。"""
        if not self._scheme:
            self._hint(tr("请先在右侧「标点方案」面板中新建方案"), 4000)
            return
        target = self.itemAt(viewport_pos)
        selected = [it for it in self._scene.selectedItems() if isinstance(it, MarkerItem)]
        if isinstance(target, MarkerItem):
            source = next((it for it in selected if it is not target), None)
            if source is not None:
                self._add_path(source.marker.id, target.marker.id)
            self._scene.clearSelection()
            target.setSelected(True)
            return
        pos = self.mapToScene(viewport_pos)
        kind = self._last_marker_type or "walk"
        marker = Marker.new(kind, max(0.0, min(1.0, pos.x() / self._img_w)),
                            max(0.0, min(1.0, pos.y() / self._img_h)))
        self._scheme.markers.append(marker)
        self._last_marker_type = kind
        self._rebuild_markers()
        self._scene.clearSelection()
        self._marker_items[marker.id].setSelected(True)
        self.win.persist_schemes()

    def _edit_clicked_marker(self, item):
        self._scene.clearSelection()
        item.setSelected(True)
        self._edit_marker()

    def _edit_marker(self):
        its = [it for it in self._scene.selectedItems() if isinstance(it, MarkerItem)]
        if not its:
            return
        it = its[0]; m = it.marker
        dlg = QDialog(self); dlg.setWindowTitle(tr("编辑标点"))
        from PySide6.QtWidgets import QFormLayout, QLineEdit, QDialogButtonBox, QTextEdit, QVBoxLayout
        form = QFormLayout()
        cb = QComboBox()
        for key, name in MARKER_TYPE_CHOICES:
            cb.addItem(tr(name), key)
        cb.setPlaceholderText(tr("保留原类型"))
        cb.setCurrentIndex(cb.findData(m.type)); form.addRow(tr("类型"), cb)
        le = QLineEdit(m.label); form.addRow(tr("标签"), le)
        te = QTextEdit(m.note or ""); form.addRow(tr("备注"), te)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept); btns.rejected.connect(dlg.reject)
        v = QVBoxLayout(); v.addLayout(form); v.addWidget(btns); dlg.setLayout(v)
        if dlg.exec():
            if cb.currentIndex() >= 0:
                m.type = cb.currentData()
            m.label = le.text(); m.note = te.toPlainText()
            it.setPixmap(self.make_marker_pixmap(m.type)); it.setToolTip(it._tip())
            self._redraw_paths()
            self.win.persist_schemes()

    def _delete_markeritem(self, it: MarkerItem):
        m = it.marker
        if self._connect_from == m.id:
            self.cancel_connect()
        self._scheme.paths = [p for p in self._scheme.paths if p.from_id != m.id and p.to_id != m.id]
        self._scheme.markers = [x for x in self._scheme.markers if x.id != m.id]
        self._rebuild_markers()
        self.win.persist_schemes()

    def _start_connect(self, src: MarkerItem):
        """列表选目标(点叠在一起点不准时的兜底)。

        列表里**只显示标签或类型名**, 不再挂一串 id(用户 2026-10-13: "只有点1 2...加一串代码, 并不直观")。
        默认入口是 `start_connect()` 的地图点选。
        """
        names: dict[str, str] = {}
        for it in self._marker_items.values():
            if it is src:
                continue
            m = it.marker
            names[m.id] = m.label or tr(MARKER_TYPES.get(m.type, ("自定义", "", ""))[0])
        if not names:
            QMessageBox.information(self, tr("提示"), tr("暂无其他标点可连接。"))
            return
        labels = list(names.values())
        sel, ok = QInputDialog.getItem(self, tr("连线到"), tr("选择目标标点:"), labels, 0, False)
        if not ok or sel not in labels:
            return
        self._add_path(src.marker.id, list(names.keys())[labels.index(sel)])

    def start_connect(self, src: MarkerItem) -> None:
        """进入"点选连线"模式: 接着在地图上点目标标点即可(用户 2026-10-13: 要更直观)。"""
        self.cancel_area_edit()
        self._connect_from = src.marker.id
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)      # 让 Esc 能取消
        self.setFocus()
        self.setDragMode(QGraphicsView.DragMode.NoDrag)      # 独占左键, 免得变成拖地图
        self._hint(tr("选择连线目标 · Esc 取消"))
        self.interaction_changed.emit()

    def cancel_connect(self) -> None:
        self._connect_from = None
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self._hint(tr("已取消连线"), 3000)
        self.interaction_changed.emit()

    def _add_path(self, from_id: str, to_id: str) -> None:
        """加一条连线(重复不加), 立即重画并落盘。"""
        if not self._scheme or not from_id or from_id == to_id:
            return
        ids = {marker.id for marker in self._scheme.markers}
        if from_id not in ids or to_id not in ids:
            return
        if not any(p.from_id == from_id and p.to_id == to_id for p in self._scheme.paths):
            self._scheme.paths.append(PathLink(from_id=from_id, to_id=to_id, enabled=True))
        self._redraw_paths()
        self.win.persist_schemes()

    def _delete_path(self, link, reverse=None) -> None:
        """删掉一条连线(用户 2026-10-13: "选错了删不掉")。"""
        if not self._scheme:
            return
        previous = self._scheme.paths
        self._scheme.paths = [p for p in previous if p is not link and p is not reverse]
        if not self.win.persist_schemes():
            self._scheme.paths = previous
        self._redraw_paths()

    def _rename_scheme(self):
        if not self._scheme:
            return
        name, ok = QInputDialog.getText(self, tr("重命名方案"), tr("方案名:"), text=self._scheme.name)
        if ok and name:
            self._scheme.name = name
            self.active_scheme_changed.emit()
            self.win.persist_schemes()

    def _new_scheme(self):
        name, ok = QInputDialog.getText(self, tr("新建方案"), tr("方案名:"))
        if not ok:
            return
        self.win.new_scheme(name or tr("未命名方案"))
