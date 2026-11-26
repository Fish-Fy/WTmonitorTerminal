"""Import a share code with a separate, read-only map preview."""
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGraphicsItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout,
)

from ..i18n import map_name, tr
from ..agent import mapimg
from ..store import share_code
from .marker_icons import OUT_PX, marker_pixmap
from .area_items import AreaItem
from .scheme_path_item import SchemePathItem, visible_link_pairs


class _Preview(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.path_items: list[SchemePathItem] = []
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setMinimumHeight(260)

    def fit(self):
        self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self._layout_paths()

    def _layout_paths(self):
        for item in self.path_items:
            item.layout(self.transform().m11(), OUT_PX / 2)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.scene().sceneRect().isEmpty():
            self.fit()

    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        if .02 < self.transform().m11() * factor < 20:
            self.scale(factor, factor)
            self._layout_paths()
        event.accept()


class ImportSchemeDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.preview_scheme = None
        self.saved_scheme = None
        self.setWindowTitle(tr("导入标点方案"))
        self.resize(820, 760)
        body = QVBoxLayout(self)
        body.addWidget(QLabel(tr("1. 选择地图")))
        self.maps = QComboBox()
        for scene, _ in mapimg.list_maps(win.data_root):
            self.maps.addItem(map_name(scene), scene)
        index = self.maps.findData(win._scene)
        if index >= 0:
            self.maps.setCurrentIndex(index)
        body.addWidget(self.maps)
        body.addWidget(QLabel(tr("2. 粘贴分享码")))
        self.code = QPlainTextEdit()
        self.code.setPlaceholderText(tr("WT1.… 至 WT5.…（支持汉字短码）"))
        self.code.setMaximumHeight(90)
        body.addWidget(self.code)
        self.preview_button = QPushButton(tr("3. 预览方案"))
        self.preview_button.clicked.connect(self.show_preview)
        body.addWidget(self.preview_button)
        self.preview = _Preview()
        body.addWidget(self.preview, 1)
        self.message = QLabel(tr("请核对标点、路线和区域与地形的位置。滚轮缩放，拖动查看。"))
        self.message.setWordWrap(True)
        body.addWidget(self.message)
        self.name = QLineEdit(tr("导入的方案"))
        self.name.setPlaceholderText(tr("新方案名称"))
        body.addWidget(self.name)
        row = QHBoxLayout()
        self.save_button = QPushButton(tr("4. 保存为新方案"))
        self.save_button.setProperty("primary", True)
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_scheme)
        close = QPushButton(tr("取消"))
        close.clicked.connect(self.reject)
        row.addWidget(self.save_button)
        row.addWidget(close)
        body.addLayout(row)
        self.maps.currentIndexChanged.connect(self.invalidate)
        self.code.textChanged.connect(self.invalidate)
        if not self.maps.count():
            self.preview_button.setEnabled(False)
            self.message.setText(tr("地图库中还没有底图，请先收录地图。"))

    def invalidate(self, *_args):
        self.preview_scheme = None
        self.save_button.setEnabled(False)
        self.preview.path_items.clear()
        self.preview.scene().clear()
        self.message.setText(tr("请点击预览，核对所选地图上的标点、路线和区域。"))

    def show_preview(self):
        self.invalidate()
        scene = self.maps.currentData()
        if not scene:
            return
        try:
            scheme = share_code.decode(self.code.toPlainText(), scene)
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        base = QPixmap(str(mapimg.map_cache_path(self.win.data_root, scene)))
        if base.isNull():
            self.message.setText(tr("所选地图的底图无法读取，请重新选择地图。"))
            return
        canvas = self.preview.scene()
        canvas.addPixmap(base)
        canvas.setSceneRect(0, 0, base.width(), base.height())
        for area in scheme.areas:
            canvas.addItem(AreaItem(area, base.width(), base.height()))
        points = {marker.id: QPointF(marker.cx * base.width(), marker.cy * base.height())
                  for marker in scheme.markers}
        types = {marker.id: marker.type for marker in scheme.markers}
        for link, reverse in visible_link_pairs(scheme.paths):
            if link.from_id in points and link.to_id in points:
                line = SchemePathItem(link, points[link.from_id], points[link.to_id],
                                      types[link.from_id], types[link.to_id], reverse)
                line.setZValue(2)
                canvas.addItem(line)
                self.preview.path_items.append(line)
        for marker in scheme.markers:
            item = canvas.addPixmap(marker_pixmap(marker.type))
            item.setOffset(-OUT_PX / 2, -OUT_PX / 2)
            item.setPos(points[marker.id])
            item.setZValue(3)
            item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
        self.preview.fit()
        self.preview_scheme = scheme
        self.save_button.setEnabled(True)
        hint = tr(" 保存后可通过「绑定任务…」为出生区选择对应任务。") if any(a.type == "rally" for a in scheme.areas) else ""
        self.message.setText(tr('{0} · {1} 个标点 · {2} 条可见连线 · {3} 个区域。确认位置后保存。{4}', scene, len(scheme.markers), sum((link.enabled for link in scheme.paths)), len(scheme.areas), hint))

    def save_scheme(self):
        scheme = self.preview_scheme
        if scheme is None:
            return
        scheme.name = self.name.text().strip() or tr("导入的方案")
        try:
            self.win.save_imported_scheme(scheme)
        except (OSError, ValueError) as exc:
            self.message.setText(tr('保存失败，可以重试。{0}', exc))
            return
        self.saved_scheme = scheme
        self.accept()
