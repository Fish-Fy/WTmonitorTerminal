"""自绘标题栏与窗口边缘；移动/缩放优先交给系统，保留正常窗口行为。"""
from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QAbstractButton, QApplication, QHBoxLayout, QLabel, QMainWindow, QWidget

from ..i18n import tr
from . import theme


class CaptionButton(QAbstractButton):
    def __init__(self, action, parent=None):
        super().__init__(parent)
        self.action = action
        self.restore = False
        self._native_hover = False
        self.setFixedSize(44, 36)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.set_label({"minimize": tr("最小化"), "maximize": tr("最大化"), "close": tr("关闭")}[action])

    def set_label(self, text):
        self.setToolTip(text)
        self.setAccessibleName(text)

    def set_native_hover(self, hover):
        if self._native_hover != hover:
            self._native_hover = hover
            self.update()

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        hover = self._native_hover or self.underMouse() or self.isDown()
        fill = theme.DANGER if hover and self.action == "close" else theme.RAISED if hover else theme.PANEL
        p.fillRect(self.rect(), QColor(fill))
        p.setPen(QPen(QColor(theme.TEXT), 1.3))
        x, y = self.width() // 2, self.height() // 2
        if self.action == "minimize":
            p.drawLine(x - 5, y + 3, x + 5, y + 3)
        elif self.action == "close":
            p.drawLine(x - 4, y - 4, x + 4, y + 4)
            p.drawLine(x - 4, y + 4, x + 4, y - 4)
        elif self.restore:
            p.drawRect(x - 2, y - 6, 8, 8)
            p.fillRect(x - 5, y - 3, 8, 8, QColor(fill))
            p.drawRect(x - 5, y - 3, 8, 8)
        else:
            p.drawRect(x - 5, y - 5, 10, 10)
        if self.hasFocus():
            p.setPen(QPen(QColor(theme.ACCENT), 1))
            p.drawRoundedRect(self.rect().adjusted(3, 3, -3, -3), 3, 3)
        p.end()


class TitleDragArea(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self._press = None
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 8, 0)
        title = QLabel(tr("WT  ·  战术终端"))
        title.setObjectName("windowTitleText")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(title)
        row.addStretch(1)
        self.setToolTip(tr("拖动移动窗口，双击最大化或还原"))

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = None
            self.window.toggle_maximized()
            event.accept()

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        handle = self.window.windowHandle()
        if handle and handle.startSystemMove():
            event.accept()
            return
        # 不支持系统移动的窗口平台使用拖动回退。
        pos = event.globalPosition().toPoint()
        if self.window.isMaximized():
            fraction = event.position().x() / max(1, self.width())
            self.window.showNormal()
            self.window.move(pos.x() - int(self.window.width() * fraction), pos.y() - 18)
        self._press = pos - self.window.pos()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.window.move(event.globalPosition().toPoint() - self._press)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._press = None
        super().mouseReleaseEvent(event)


def resized_rect(rect, delta, edges, minimum, maximum):
    """回退缩放：钉住对边，遵守 Qt 布局的最小/最大尺寸。"""
    result = QRect(rect)
    if edges & Qt.Edge.LeftEdge:
        width = min(maximum.width(), max(minimum.width(), rect.width() - delta.x()))
        result.setLeft(rect.right() - width + 1)
    elif edges & Qt.Edge.RightEdge:
        result.setWidth(min(maximum.width(), max(minimum.width(), rect.width() + delta.x())))
    if edges & Qt.Edge.TopEdge:
        height = min(maximum.height(), max(minimum.height(), rect.height() - delta.y()))
        result.setTop(rect.bottom() - height + 1)
    elif edges & Qt.Edge.BottomEdge:
        result.setHeight(min(maximum.height(), max(minimum.height(), rect.height() + delta.y())))
    return result


class ResizeEdge(QWidget):
    def __init__(self, window, edges, cursor):
        super().__init__(window)
        self.window = window
        self.edges = edges
        self._press = None
        self.setCursor(cursor)
        self.setObjectName("windowResizeEdge")
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or self.window.isMaximized():
            return
        handle = self.window.windowHandle()
        if not handle or not handle.startSystemResize(self.edges):
            self._press = (event.globalPosition().toPoint(), self.window.geometry())
        event.accept()

    def mouseMoveEvent(self, event):
        if self._press and event.buttons() & Qt.MouseButton.LeftButton:
            point, rect = self._press
            minimum = self.window.minimumSize().expandedTo(self.window.minimumSizeHint())
            self.window.setGeometry(resized_rect(rect, event.globalPosition().toPoint() - point,
                                                self.edges, minimum, self.window.maximumSize()))
            event.accept()

    def mouseReleaseEvent(self, event):
        self._press = None
        super().mouseReleaseEvent(event)


class FramelessWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._native_frame = None
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.title_bar = QWidget(self)
        self.title_bar.setObjectName("windowTitleBar")
        self.title_bar.setFixedHeight(36)
        row = QHBoxLayout(self.title_bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.title_drag = TitleDragArea(self)
        row.addWidget(self.title_drag, 1)
        self.caption_buttons = {}
        for name, callback in (("minimize", self.showMinimized), ("maximize", self.toggle_maximized),
                               ("close", lambda: self.close())):
            button = CaptionButton(name, self.title_bar)
            button.clicked.connect(callback)
            row.addWidget(button)
            self.caption_buttons[name] = button
        self.setMenuWidget(self.title_bar)
        E, C = Qt.Edge, Qt.CursorShape
        specs = ((E.LeftEdge, C.SizeHorCursor), (E.RightEdge, C.SizeHorCursor),
                 (E.TopEdge, C.SizeVerCursor), (E.BottomEdge, C.SizeVerCursor),
                 (E.TopEdge | E.LeftEdge, C.SizeFDiagCursor),
                 (E.TopEdge | E.RightEdge, C.SizeBDiagCursor),
                 (E.BottomEdge | E.LeftEdge, C.SizeBDiagCursor),
                 (E.BottomEdge | E.RightEdge, C.SizeFDiagCursor))
        self.resize_edges = [ResizeEdge(self, edges, cursor) for edges, cursor in specs]
        self.statusBar().setSizeGripEnabled(False)

    def showEvent(self, event):
        super().showEvent(event)
        if QApplication.platformName() == "windows":
            if self._native_frame is None:
                from .windows_frame import WindowsFrame
                self._native_frame = WindowsFrame(self)
            self._native_frame.install()

    def nativeEvent(self, event_type, message):
        frame = getattr(self, "_native_frame", None)
        if frame is not None and event_type == b"windows_generic_MSG":
            handled, result = frame.handle(message)
            if handled:
                return True, result
        return super().nativeEvent(event_type, message)

    def toggle_maximized(self):
        self.showNormal() if self.isMaximized() else self.showMaximized()

    def _place_edges(self):
        if not hasattr(self, "resize_edges"):
            return
        w, h, b, corner = self.width(), self.height(), 5, 10
        rects = (QRect(0, corner, b, h - 2 * corner), QRect(w - b, corner, b, h - 2 * corner),
                 QRect(corner, 0, w - 2 * corner, b), QRect(corner, h - b, w - 2 * corner, b),
                 QRect(0, 0, corner, corner), QRect(w - corner, 0, corner, corner),
                 QRect(0, h - corner, corner, corner), QRect(w - corner, h - corner, corner, corner))
        for edge, rect in zip(self.resize_edges, rects):
            edge.setGeometry(rect)
            edge.setVisible(not (self.isMaximized() or self.isFullScreen()))
            edge.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_edges()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "caption_buttons"):
            button = self.caption_buttons["maximize"]
            button.set_native_hover(False)
            button.restore = self.isMaximized()
            button.set_label(tr("还原") if button.restore else tr("最大化"))
            button.update()
            self._place_edges()
