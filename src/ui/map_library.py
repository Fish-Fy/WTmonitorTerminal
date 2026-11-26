"""地图库: **缩略图选图** + 类型筛选 + 重复合并 + 离线规划。

8111 没有地图清单和名称接口。地图库支持对局底图缓存，也支持导入公开底图及任务简报布局。
离线规划的口径是：
  * 有底图的地图 → 直接用 `data/maps/<scene>/base.png`，列表里显示它的**缩略图**；
  * 无底图目录自动清理，重复底图自动合并并优先保留人工名字。
用户 2026-10-12 追加诉求：
  * 库里混进了**空战图** ⇒ 按类型筛选，默认只看「陆战 + 未分类」(空战单独一档);
  * 同一张图因几何伪名或分辨率变化被存成多个目录 ⇒ 按整幅底图比对合并。
类型判据见 mapimg.guess_kind，写在 data/maps/<scene>/meta.json。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal, QSignalBlocker
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout, QWidget, QMenu, QStyledItemDelegate, QStyleOptionViewItem, QStyle,
)

from ..i18n import map_name, matches_map, tr
from ..agent import mapimg
from ..agent.task_layout import layout_modes
from ..store import markers as markers_store
from .task_filter import mission_name, task_mode_combo

CANVAS_PX = 2048          # 网格画布尺寸(与真底图同量级, 归一化坐标才对得上)
GRID_PX = 128             # 一格 128px = 图幅的 1/16
THUMB_PX = 132            # 缩略图边长(对话框与右侧面板共用)
_LAYOUT_INDEX = Qt.ItemDataRole.UserRole + 1

# 筛选档位: key -> 显示名。"ground" 含**未分类**(历史缓存还没判过类型, 不能让它消失)
FILTERS: tuple[tuple[str, str], ...] = (
    ("ground", "陆战"),
    ("both", "陆战+空战"),
    ("air", "空战"),
    ("naval", "海战"),
    ("unknown", "未分类（还没判过）"),
    ("all", "全部"),
)


def offline_canvas(scene: str, size: int = CANVAS_PX, step: int = GRID_PX) -> QImage:
    """没有底图时用的网格画布(深色底 + 分级网格 + 角标)。"""
    img = QImage(size, size, QImage.Format.Format_RGB32)
    img.fill(QColor("#1b2430"))
    p = QPainter(img)
    try:
        p.setPen(QPen(QColor("#2c3a4a"), 1))
        for i in range(0, size + 1, step):
            p.drawLine(i, 0, i, size)
            p.drawLine(0, i, size, i)
        p.setPen(QPen(QColor("#43566b"), 2))
        for i in range(0, size + 1, step * 4):     # 每 4 格一根亮线, 方便估距离
            p.drawLine(i, 0, i, size)
            p.drawLine(0, i, size, i)
        f = QFont()
        f.setPixelSize(max(12, size // 18))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#8aa0b6"))
        p.drawText(16, max(28, size // 12), tr('{0} · 网格', scene))
    finally:
        p.end()
    return img


def map_thumbnail(data_root: Path, scene: str, size: int = THUMB_PX) -> QPixmap:
    """地图缩略图。**解码即缩放 + 磁盘缓存**（用户 2026-10-12 报"启动变慢了许多"）。

    两个性能坑：
      * `QImage(base.png)` 会把 2048²/4096² 的底图**整张解码**(16MB/64MB 位图)再缩小 ——
        实测单张 ~23ms, 几十张就上秒; 改用 `QImageReader.setScaledSize` 让解码器**直接产出
        小图**(单张 ~4ms, 快 6 倍);
      * 每次启动都重算更亏 ⇒ 缩略图落盘成同目录的 `thumb.png`(约 10KB), 之后直接读它;
        底图更新过(mtime 更新)才重新生成。
    """
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QImageReader
    base = mapimg.map_cache_path(data_root, scene)
    if base.is_file():
        thumb = base.with_name("thumb.png")
        try:
            if thumb.is_file() and thumb.stat().st_mtime >= base.stat().st_mtime:
                pm = QPixmap(str(thumb))
                if not pm.isNull():
                    return pm
        except OSError:
            pass
        reader = QImageReader(str(base))
        orig = reader.size()
        if orig.isValid() and orig.width() > 0 and orig.height() > 0:
            k = min(size / orig.width(), size / orig.height())      # 等比, 别拉变形
            reader.setScaledSize(QSize(max(1, round(orig.width() * k)),
                                       max(1, round(orig.height() * k))))
        img = reader.read()
        if not img.isNull():
            pm = QPixmap.fromImage(img)
            try:
                pm.save(str(thumb), "PNG")          # 下次直接读这个小的
            except Exception:                        # noqa: BLE001
                pass
            return pm
    pm = QPixmap.fromImage(offline_canvas(scene, size=512, step=64))
    return pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatio,
                     Qt.TransformationMode.SmoothTransformation)


class _MapThumbnails(QStyledItemDelegate):
    """地图列表只为当前画面请求缩略图，滚动或搜索后再加载新出现的地图。"""

    def __init__(self, view):
        super().__init__(view)
        self.icons = {}

    def paint(self, painter, option, index):
        scene = index.data(Qt.ItemDataRole.UserRole)
        if scene not in self.icons:
            self.icons[scene] = QIcon(map_thumbnail(self.data_root, scene))
        style_option = QStyleOptionViewItem(option)
        self.initStyleOption(style_option, index)
        style_option.icon = self.icons[scene]
        style_option.features |= QStyleOptionViewItem.ViewItemFeature.HasDecoration
        style_option.widget.style().drawControl(QStyle.ControlElement.CE_ItemViewItem, style_option,
                                               painter, style_option.widget)


def _icon_list(parent: QWidget) -> QListWidget:
    """统一的"缩略图格子"列表(对话框与右侧面板共用)。"""
    w = QListWidget(parent)
    w.setViewMode(QListWidget.ViewMode.IconMode)
    w.setIconSize(QSize(THUMB_PX, THUMB_PX))
    w.setGridSize(QSize(THUMB_PX + 30, THUMB_PX + 54))
    w.setResizeMode(QListWidget.ResizeMode.Adjust)
    w.setMovement(QListWidget.Movement.Static)
    w.setWordWrap(True)
    return w


def _kind_label(ks: list[str]) -> str:
    """类型标签。同一张图可能两种模式都用得到(用户 2026-10-12 指出的"两用图")。"""
    if not ks:
        return tr("未分类")
    labels = {"ground": tr("陆战"), "air": tr("空战"), "naval": tr("海战")}
    return "+".join(label for key, label in labels.items() if key in ks) or tr("未分类")


def maps_for_filter(data_root: Path, kind: str = "ground",
                    skip_dups: bool = False) -> list[tuple[str, Path | None]]:
    """按筛选档位取地图列表。

    * ground 档 = 带 ground 标签的图 **加未分类**(历史缓存不能凭空消失);
    * air 档 = 只列带 air 标签的(未分类的不往空战档里塞);
    * **两用图**(陆战局里上飞机切到的空中大地图 = 空战模式的图)在**两个档里都会出现**。

    ⚠ `skip_dups` 默认 **False**: 它按整幅底图去重, 会把"与另一张图内容相同"的那张**藏起来**
    —— 用户 2026-10-17 把一张图改名成「库班」后怎么也找不到, 就是被当成重复项跳过了。
    去重改成显式操作(地图库的「合并重复地图…」), 列表默认**全部列出**。
    """
    allm = mapimg.list_maps(data_root, kind=None, skip_dups=skip_dups)
    if kind == "all":
        return allm
    # 2026-10-17 用户定稿：按**标签组合**精确归档，一张图只属于一个档：
    #   陆战 = 只有 ground；陆战+空战 = 两者都有；空战 = 只有 air；未分类 = 都没有。
    if kind == "air":
        return [x for x in allm if set(mapimg.map_kinds(data_root, x[0])) == {"air"}]
    if kind == "naval":
        return [x for x in allm if "naval" in mapimg.map_kinds(data_root, x[0])]
    if kind == "both":
        return [x for x in allm
                if set(mapimg.map_kinds(data_root, x[0])) == {"ground", "air"}]
    if kind == "unknown":
        return [x for x in allm if not mapimg.map_kinds(data_root, x[0])]
    return [x for x in allm if set(mapimg.map_kinds(data_root, x[0])) == {"ground"}]


def fill_map_items(lst: QListWidget, data_root: Path, kind: str = "ground",
                   skip_dups: bool = False) -> None:
    """把列表填成"缩略图 + 名字"的格子; 文本 = 场景名 / 类型·底图·方案数。"""
    lst.clear()
    delegate = lst.itemDelegate()
    if not isinstance(delegate, _MapThumbnails):
        delegate = _MapThumbnails(lst)
        lst.setItemDelegate(delegate)
    delegate.data_root = data_root
    delegate.icons.clear()
    for scene, base in maps_for_filter(data_root, kind, skip_dups):
        n = len(markers_store.load_schemes(data_root, scene))
        kind_txt = _kind_label(mapimg.map_kinds(data_root, scene))
        src = tr("底图") if base else tr("无底图·网格")
        rej = mapimg.read_meta(data_root, scene).get("base_rejected")
        label = map_name(scene)
        it = QListWidgetItem(tr('{0}\n{1} · {2} · {3} 方案', label, kind_txt, src, n))
        it.setSizeHint(lst.gridSize())
        it.setData(Qt.ItemDataRole.UserRole, scene)
        tip = tr('{0}（{1}，{2}，{3} 个方案）\n双击载入规划；类型可在「地图库…」里校正',
                 label, kind_txt, src, n)
        if rej:
            tip += tr('\n⚠ 这张图的底图被拒过（尺寸 {0}），所以只有网格画布', rej)
        it.setToolTip(tip)
        lst.addItem(it)


class _KindFilterMixin:
    """给"缩略图列表"加一个类型筛选下拉(默认陆战+未分类)。"""

    def _build_filter_row(self, layout: QHBoxLayout, data_root: Path) -> QComboBox:
        combo = QComboBox()
        for key, label in FILTERS:
            combo.addItem(tr(label), key)
        combo.setToolTip(tr("只看你关心的类型（空战图不再混进来）"))
        combo.setCurrentIndex(0)
        combo.currentIndexChanged.connect(lambda _i: self.refresh())
        layout.addWidget(QLabel(tr("类型:")))
        layout.addWidget(combo, 1)
        return combo

    def _current_kind(self) -> str:
        combo = getattr(self, "kind_combo", None)
        return str(combo.currentData() or "ground") if combo is not None else "ground"

    def _build_search(self, layout: QVBoxLayout) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(tr("按地图名字搜索…"))
        edit.setClearButtonEnabled(True)
        edit.setToolTip(tr("输入部分地图名即可搜索，不区分大小写；在当前类型内查找，跨类型请选「全部」"))
        edit.textChanged.connect(self._apply_search)
        layout.addWidget(edit)
        return edit

    def _apply_search(self, *_args) -> None:
        # 只隐藏条目，输入时不重新读取磁盘或生成缩略图。
        query = self.search_edit.text().strip().casefold()
        first = None
        for i in range(self.list.count()):
            item = self.list.item(i)
            scene = str(item.data(Qt.ItemDataRole.UserRole) or "")
            visible = matches_map(scene, query)
            item.setHidden(not visible)
            if visible and first is None:
                first = item
        current = self.list.currentItem()
        if current is None or current.isHidden():
            self.list.clearSelection()
            if first is None:
                self.list.setCurrentRow(-1)
            else:
                self.list.setCurrentItem(first)
        if hasattr(self, "_update_selection"):
            self._update_selection()


class MapPickerPanel(QWidget, _KindFilterMixin):
    """右列的「地图」面板: 离线规划时**顶掉载具识别**的位置, 点缩略图即切图。"""

    map_chosen = Signal(str)

    def __init__(self, data_root: Path, parent=None):
        super().__init__(parent)
        self.data_root = data_root
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 2, 4, 4)
        row = QHBoxLayout()
        self.kind_combo = self._build_filter_row(row, data_root)
        v.addLayout(row)
        self.search_edit = self._build_search(v)
        self.list = _icon_list(self)
        self.list.itemClicked.connect(self._clicked)
        v.addWidget(self.list, 1)
        v.addWidget(QLabel(tr("点缩略图即切到该图；改名/归类用「地图库…」")))
        self._dirty = True       # 启动时**不**生成缩略图(几十张大图解码会拖慢启动), 首次显示才刷

    def showEvent(self, e):
        if self._dirty:                       # 懒加载: 真要看了才生成
            self._dirty = False
            self.refresh()
        super().showEvent(e)

    def refresh(self) -> None:
        self._dirty = False
        mapimg.maintain_library(self.data_root)
        fill_map_items(self.list, self.data_root, self._current_kind())
        self._apply_search()

    def scene_at(self, item: QListWidgetItem) -> str | None:
        s = item.data(Qt.ItemDataRole.UserRole)
        return None if s is None else str(s)

    def _clicked(self, item: QListWidgetItem) -> None:
        scene = self.scene_at(item)
        if scene:
            self.map_chosen.emit(scene)


class MapLibrarySidebar(QWidget):
    """共享方案面板与地图库编辑器，选图后仍能新建/切换标点方案。"""

    def __init__(self, win):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.editor = MapLibraryDialog(win, self, embedded=True)
        layout.addWidget(self.editor, 1)

    def refresh(self):
        self.editor.refresh()


class MapLibraryDialog(QDialog, _KindFilterMixin):
    """地图库：缩略图里选一张 → 载入规划（离线也能用）。"""

    map_chosen = Signal(str, int)

    def __init__(self, win, parent=None, *, embedded=False):
        super().__init__(parent)
        self.win = win
        self.embedded = embedded
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.chosen: str | None = None
        self.setWindowTitle(tr("地图库（离线规划）"))
        self.resize(700, 600)

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 6, 8, 6)
        row = QHBoxLayout()
        self.kind_combo = self._build_filter_row(row, win.data_root)
        v.addLayout(row)
        self.search_edit = self._build_search(v)
        self.list = _icon_list(self)
        self.list.itemDoubleClicked.connect(lambda _i: self._load())
        if embedded:
            self.list.setIconSize(QSize(110, 110))
            self.list.setGridSize(QSize(132, 164))
            self.list.setMinimumHeight(164)
        v.addWidget(self.list, 1)
        self.result_hint = QLabel()
        self.result_hint.setProperty("muted", True)
        self.result_hint.setWordWrap(True)
        v.addWidget(self.result_hint)
        self.show_all_button = QPushButton(tr("查看全部地图"))
        self.show_all_button.clicked.connect(self._show_all)
        v.addWidget(self.show_all_button)

        self.task_mode = task_mode_combo()
        self.task_mode.currentIndexChanged.connect(self._filter_layouts)
        v.addWidget(self.task_mode)
        self.layout_combo = QComboBox()
        self.layout_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.layout_combo.setToolTip(tr("切换任务即载入对应的战区和出生点。蓝色为一方，红色为另一方。"))
        self.layout_combo.activated.connect(self._select_layout)
        v.addWidget(self.layout_combo)

        row2 = QHBoxLayout()
        self.load_button = QPushButton(tr("载入规划"))
        self.load_button.setProperty("primary", True)
        self.load_button.clicked.connect(self._load)
        self.rename_button = QPushButton(tr("重命名…"))
        self.rename_button.clicked.connect(self._rename)
        row2.addWidget(self.load_button, 1)
        row2.addWidget(self.rename_button, 1)
        v.addLayout(row2)
        manage = QPushButton(tr("地图库管理"))
        menu = QMenu(manage)
        self.classify_menu = menu.addMenu(tr("修改分类"))
        self.kind_actions = {}
        for key, label in FILTERS:
            if key == "all":
                continue
            action = self.classify_menu.addAction(tr(label))
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, kind=key: self._mark(kind))
            self.kind_actions[key] = action
        self.delete_action = menu.addAction(tr("删除所选地图…"), self._delete_map)
        menu.addSeparator()
        menu.addAction(tr("立即整理地图库"), self._merge_dups)
        menu.addAction(tr("全部未分类地图设为陆战"), self._mark_all_ground)
        manage.setMenu(menu)
        v.addWidget(manage)
        if not embedded:
            close = QPushButton(tr("关闭"))
            close.clicked.connect(self.reject)
            v.addWidget(close)
        self.list.currentItemChanged.connect(self._update_selection)

        self._reload()

    def _show_all(self):
        with QSignalBlocker(self.search_edit), QSignalBlocker(self.kind_combo):
            self.search_edit.clear()
            self.kind_combo.setCurrentIndex(self.kind_combo.findData("all"))
        self._reload()

    def select_displayed_task(self) -> None:
        scene = self.win._scene
        if scene is None:
            return
        for index in range(self.list.count()):
            item = self.list.item(index)
            if not item.isHidden() and item.data(Qt.ItemDataRole.UserRole) == scene:
                self.list.setCurrentItem(item)
                break
        else:
            return
        task = self.win._task
        if task is None:
            return
        if self.layout_combo.findData(task.id) < 0 and self.task_mode.currentData() != "all":
            with QSignalBlocker(self.task_mode):
                self.task_mode.setCurrentIndex(self.task_mode.findData("all"))
        self._populate_layouts(task.id)

    def _update_selection(self, *_args):
        if not hasattr(self, "classify_menu"):
            return
        scene = self._current()
        previous_id = self.layout_combo.currentData() if scene == getattr(self, "_layout_scene", None) else None
        self._layout_scene = scene
        self._layouts = mapimg.read_meta(self.win.data_root, scene).get("public_layouts", []) if scene else []
        self._populate_layouts(previous_id)
        for control in (self.load_button, self.rename_button, self.classify_menu):
            control.setEnabled(scene is not None)
        self.delete_action.setEnabled(scene is not None)
        kinds = set(mapimg.map_kinds(self.win.data_root, scene)) if scene else set()
        kind = "both" if kinds == {"ground", "air"} else next(iter(kinds), "unknown")
        for key, action in self.kind_actions.items():
            action.setChecked(scene is not None and key == kind)
        count = sum(not self.list.item(i).isHidden() for i in range(self.list.count()))
        manual = bool(mapimg.read_meta(self.win.data_root, scene).get("manual")) if scene else False
        source = tr("手动分类") if manual else tr("自动分类")
        self.result_hint.setText(tr('{0} 张地图 · {1}', count, source) if count else
                                 tr("没有匹配的地图"))
        self.show_all_button.setVisible(count == 0 and
                                        (bool(self.search_edit.text()) or self._current_kind() != "all"))

    def _populate_layouts(self, previous_id) -> None:
        mode = self.task_mode.currentData()
        with QSignalBlocker(self.layout_combo):
            self.layout_combo.clear()
            self.layout_combo.setPlaceholderText(tr("此模式没有任务"))
            for index, layout in enumerate(self._layouts):
                if mode != "all" and mode not in layout_modes(layout):
                    continue
                row = self.layout_combo.count()
                label = mission_name(layout["name"])
                self.layout_combo.addItem(label, layout.get("id", index))
                self.layout_combo.setItemData(row, index, _LAYOUT_INDEX)
                self.layout_combo.setItemData(row, label, Qt.ItemDataRole.ToolTipRole)
            if self.layout_combo.count():
                self.layout_combo.setCurrentIndex(max(0, self.layout_combo.findData(previous_id)))
        self.task_mode.setVisible(bool(self._layouts))
        self.layout_combo.setVisible(bool(self._layouts))
        self.layout_combo.setEnabled(self.layout_combo.count() > 0)

    def _filter_layouts(self, _index: int) -> None:
        previous_id = self.layout_combo.currentData()
        self._populate_layouts(previous_id)
        if (self.embedded and self._current() and self._current() == self.win._scene
                and self.win._map_locked and self.layout_combo.currentData() != previous_id):
            self.win.show_public_layout(self._selected_layout_index())

    def _selected_layout_index(self) -> int:
        index = self.layout_combo.currentData(_LAYOUT_INDEX)
        return index if index is not None else -1

    # ------------------------------------------------------------------ 数据
    def _reload(self, report=None) -> None:
        selected = self._current() or getattr(self.win, "_scene", None)
        maintain = getattr(self.win, "maintain_map_library", None)
        if report is None:
            report = maintain() if maintain else mapimg.maintain_library(self.win.data_root)
        self._last_maintenance = report
        for keep, sources in report["merged"]:
            if selected in sources:
                selected = keep
        with QSignalBlocker(self.list):
            fill_map_items(self.list, self.win.data_root, self._current_kind())
            for i in range(self.list.count()):
                item = self.list.item(i)
                if item.data(Qt.ItemDataRole.UserRole) == selected:
                    self.list.setCurrentItem(item)
                    break
            self._apply_search()

    def refresh(self) -> None:                      # 筛选下拉回调
        if hasattr(self, "list"):
            self._reload()

    def _current(self) -> str | None:
        it = self.list.currentItem()
        if it is None or it.isHidden():
            return None
        s = it.data(Qt.ItemDataRole.UserRole)
        return None if s is None else str(s)

    # ------------------------------------------------------------------ 动作
    def _select_layout(self, _index: int) -> None:
        if not self.embedded:
            return
        scene = self._current()
        if scene and scene == self.win._scene and self.win._map_locked:
            self.win.show_public_layout(self._selected_layout_index())
        else:
            self._load()

    def _load(self) -> None:
        scene = self._current()
        if not scene:
            return
        self.chosen = scene
        layout_index = self._selected_layout_index()
        if self.embedded:
            self.map_chosen.emit(scene, layout_index)
        else:
            self.accept()

    def _rename(self) -> None:
        scene = self._current()
        if not scene:
            return
        name, ok = QInputDialog.getText(self, tr("重命名地图"), tr("新名字（自己认得出就行）："), text=scene)
        name = (name or "").strip()
        if not ok or not name or name == scene:
            return
        if hasattr(self.win, "rename_map") and self.win.rename_map(scene, name):
            # 搜索的原名称在改名后不再匹配，清空它才能继续看到刚改名的地图。
            if not matches_map(name, self.search_edit.text().strip().casefold()):
                with QSignalBlocker(self.search_edit):
                    self.search_edit.clear()
            self._reload()
            for i in range(self.list.count()):
                if self.list.item(i).data(Qt.ItemDataRole.UserRole) == name:
                    self.list.setCurrentItem(self.list.item(i))
                    break

    def _delete_map(self) -> None:
        scene = self._current()
        if not scene:
            return
        answer = QMessageBox.question(
            self, tr("删除地图"),
            tr('从地图库删除「{0}」？\n\n底图、缩略图和任务布局会移到本地备份，个人标点方案会保留。\n以后在游戏中遇到这张地图时，可能会重新收录。', scene),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            mapimg.remove_library_map(self.win.data_root, scene)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, tr("删除地图"), tr('无法删除「{0}」：{1}', scene, exc))
            return
        if self.embedded:
            if self.win._scene == scene and self.win._map_locked:
                self.win.choose_map(scene)
            self.win.statusBar().showMessage(tr('已从地图库删除「{0}」', scene), 5000)
        self._reload()

    def _mark(self, kind: str) -> None:
        """手动校正类型(自动判据只管"出现过", 校正了就以你为准, 不再被覆盖)。"""
        scene = self._current()
        if not scene:
            return
        try:
            mapimg.set_map_kind(self.win.data_root, scene, kind)
        except OSError as exc:
            self._update_selection()
            QMessageBox.warning(self, tr("分类未保存"), str(exc))
            return
        self._reload()
        # 分类后保持该地图可见，避免操作刚完成，目标就被旧筛选藏起来。
        if self._current_kind() not in (kind, "all"):
            self.kind_combo.setCurrentIndex(self.kind_combo.findData(kind))
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == scene:
                self.list.setCurrentItem(self.list.item(i))
                break

    def _mark_all_ground(self) -> None:
        """把还没有类型的图一次性标成陆战（历史缓存没记录过类型, 一个个点太累）。"""
        root = self.win.data_root
        n = 0
        for scene, _base in mapimg.list_maps(root, kind="unknown"):
            if scene in mapimg.DUP_SKIP:
                continue
            try:
                mapimg.set_map_kind(root, scene, "ground")
            except OSError as exc:
                QMessageBox.warning(self, tr("分类未保存"), tr('已完成 {0} 张，{1} 保存失败：{2}', n, scene, exc))
                self._reload()
                return
            n += 1
        QMessageBox.information(
            self, tr("标记完成"),
            tr('已把 {0} 张未分类地图标为陆战。\n两用地图可在「地图库管理 → 修改分类」中选择「陆战+空战」。', n))
        self._reload()

    def _merge_dups(self) -> None:
        # 整理已在启动、刷新和缓存时自动执行，按钮只是立即重新检查。
        self._reload()
        report = self._last_maintenance
        if report.get("pending"):
            self.win._library_notify = True
            self.win.statusBar().showMessage(tr("正在后台整理地图库，可以继续使用地图"), 5000)
            return
        merged = sum(len(sources) for _keep, sources in report["merged"])
        removed = len(report["removed"])
        QMessageBox.information(self, tr("地图库整理"),
                                tr('本次合并 {0} 张重复地图，清理 {1} 个无底图目录。\n人工名字和方案保留；无法安全处理的项目会保留并记录原因。', merged, removed))
