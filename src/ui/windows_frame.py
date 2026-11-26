"""Windows 原生窗口行为桥接：保留自绘外观，同时参与系统贴靠布局。

只有 Qt windows 平台才创建桥接；非 Windows/离屏测试仍使用 Qt 回退。
https://learn.microsoft.com/windows/apps/desktop/modernize/ui/apply-snap-layout-menu
"""
import ctypes
from ctypes import wintypes

from PySide6.QtCore import QPoint


WM_NCCALCSIZE = 0x0083
WM_NCHITTEST = 0x0084
WM_NCMOUSEMOVE = 0x00A0
WM_NCLBUTTONDOWN, WM_NCLBUTTONUP, WM_NCLBUTTONDBLCLK = 0x00A1, 0x00A2, 0x00A3
WM_MOUSEMOVE, WM_LBUTTONUP, WM_CAPTURECHANGED = 0x0200, 0x0202, 0x0215
WM_NCMOUSELEAVE = 0x02A2
HTCLIENT, HTCAPTION, HTMAXBUTTON = 1, 2, 9
HTLEFT, HTRIGHT, HTTOP, HTTOPLEFT, HTTOPRIGHT = 10, 11, 12, 13, 14
HTBOTTOM, HTBOTTOMLEFT, HTBOTTOMRIGHT = 15, 16, 17
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_SYSMENU = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WS_POPUP = 0x80000000
SNAP_STYLES = WS_CAPTION | WS_THICKFRAME | WS_SYSMENU | WS_MINIMIZEBOX | WS_MAXIMIZEBOX


class MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


class TrackMouseEvent(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


def screen_point(lparam):
    """WM_NCHITTEST 坐标为带符号 16 位，左侧/上方副屏不能按无符号读。"""
    return wintypes.POINT(ctypes.c_short(lparam & 0xFFFF).value,
                          ctypes.c_short((lparam >> 16) & 0xFFFF).value)


def hit_test(window, point):
    """Qt 逻辑坐标命中；内容和普通按钮仍交给 Qt，自绘最大化按钮交给 Windows。"""
    if window.isFullScreen():
        return HTCLIENT
    if not window.isMaximized():
        x, y, w, h = point.x(), point.y(), window.width(), window.height()
        b, corner = 5, 10
        left, right, top, bottom = x < b, x >= w - b, y < b, y >= h - b
        if (left and y < corner) or (top and x < corner):
            return HTTOPLEFT
        if (right and y < corner) or (top and x >= w - corner):
            return HTTOPRIGHT
        if (left and y >= h - corner) or (bottom and x < corner):
            return HTBOTTOMLEFT
        if (right and y >= h - corner) or (bottom and x >= w - corner):
            return HTBOTTOMRIGHT
        if left:
            return HTLEFT
        if right:
            return HTRIGHT
        if top:
            return HTTOP
        if bottom:
            return HTBOTTOM
    button = window.caption_buttons["maximize"]
    if button.isVisible() and button.isEnabled() and button.rect().contains(button.mapFrom(window, point)):
        return HTMAXBUTTON
    drag = window.title_drag
    if drag.isVisible() and drag.rect().contains(drag.mapFrom(window, point)):
        return HTCAPTION
    return HTCLIENT


class WindowsFrame:
    def __init__(self, window):
        self.window = window
        self._maximize_pressed = False
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        u = self.user32
        ptr = ctypes.c_ssize_t
        # HWND/LRESULT 不能使用 ctypes 默认的 32 位 int，64 位进程会截断。
        u.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowLongPtrW.restype = ptr
        u.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ptr]
        u.SetWindowLongPtrW.restype = ptr
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.SetWindowPos.restype = wintypes.BOOL
        u.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        u.ScreenToClient.restype = wintypes.BOOL
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.IsZoomed.restype = wintypes.BOOL
        u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u.MonitorFromWindow.restype = wintypes.HMONITOR
        u.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)]
        u.GetMonitorInfoW.restype = wintypes.BOOL
        u.TrackMouseEvent.argtypes = [ctypes.POINTER(TrackMouseEvent)]
        u.TrackMouseEvent.restype = wintypes.BOOL
        u.SetCapture.argtypes = [wintypes.HWND]
        u.SetCapture.restype = wintypes.HWND
        u.ReleaseCapture.argtypes = []
        u.ReleaseCapture.restype = wintypes.BOOL
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = ptr
        self.dwmapi.DwmDefWindowProc.argtypes = [wintypes.HWND, wintypes.UINT,
                                                wintypes.WPARAM, wintypes.LPARAM, ctypes.POINTER(ptr)]
        self.dwmapi.DwmDefWindowProc.restype = wintypes.BOOL

    def install(self):
        hwnd = int(self.window.winId())
        style = self.user32.GetWindowLongPtrW(hwnd, GWL_STYLE)
        target = (style | SNAP_STYLES) & ~WS_POPUP
        if style == target:
            return
        ctypes.set_last_error(0)
        previous = self.user32.SetWindowLongPtrW(hwnd, GWL_STYLE, target)
        if not previous and ctypes.get_last_error():
            raise ctypes.WinError(ctypes.get_last_error())
        # 只让系统重算非客户区，不移动、不激活窗口。
        if not self.user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0020 | 0x0001 | 0x0002 | 0x0004 | 0x0010):
            raise ctypes.WinError(ctypes.get_last_error())

    def handle(self, message):
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_NCCALCSIZE:
            # NCCALCSIZE_PARAMS 的第一个字段就是新窗口 RECT；wParam=0 时也是 RECT。
            # 正常窗口客户区铺满，最大化时裁到当前显示器工作区，避免遮住任务栏。
            if msg.lParam and not self.window.isFullScreen() and self.user32.IsZoomed(msg.hWnd):
                info = MonitorInfo()
                info.cbSize = ctypes.sizeof(info)
                monitor = self.user32.MonitorFromWindow(msg.hWnd, 2)
                if self.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                    rect = wintypes.RECT.from_address(msg.lParam)
                    rect.left, rect.top = max(rect.left, info.rcWork.left), max(rect.top, info.rcWork.top)
                    rect.right, rect.bottom = min(rect.right, info.rcWork.right), min(rect.bottom, info.rcWork.bottom)
            return True, 0
        if msg.message == WM_NCHITTEST:
            point = screen_point(msg.lParam)
            if self.user32.ScreenToClient(msg.hWnd, ctypes.byref(point)):
                ratio = self.window.devicePixelRatioF()
                return True, hit_test(self.window, QPoint(int(point.x / ratio), int(point.y / ratio)))
        if msg.message in (WM_NCLBUTTONDOWN, WM_NCLBUTTONDBLCLK) and msg.wParam == HTMAXBUTTON:
            # HTMAXBUTTON exposes the Snap menu. Qt still owns the actual custom button.
            self._maximize_pressed = True
            self.window.caption_buttons["maximize"].setDown(True)
            self.user32.SetCapture(msg.hWnd)
            return True, 0
        if self._maximize_pressed:
            button = self.window.caption_buttons["maximize"]
            if msg.message == WM_CAPTURECHANGED:
                self._maximize_pressed = False
                button.setDown(False)
            elif msg.message in (WM_MOUSEMOVE, WM_LBUTTONUP, WM_NCLBUTTONUP):
                point = screen_point(msg.lParam)
                has_point = msg.message != WM_NCLBUTTONUP or self.user32.ScreenToClient(
                    msg.hWnd, ctypes.byref(point))
                ratio = self.window.devicePixelRatioF()
                inside = has_point and hit_test(
                    self.window, QPoint(int(point.x / ratio), int(point.y / ratio))) == HTMAXBUTTON
                button.setDown(inside)
                if msg.message != WM_MOUSEMOVE:
                    self._maximize_pressed = False
                    button.setDown(False)
                    self.user32.ReleaseCapture()
                    if inside:
                        button.click()
                return True, 0
        if msg.message in (WM_NCMOUSEMOVE, WM_NCMOUSELEAVE):
            hover = msg.message == WM_NCMOUSEMOVE and msg.wParam == HTMAXBUTTON
            self.window.caption_buttons["maximize"].set_native_hover(hover)
            if hover:
                tracking = TrackMouseEvent(ctypes.sizeof(TrackMouseEvent), 0x0002 | 0x0010, msg.hWnd, 0)
                self.user32.TrackMouseEvent(ctypes.byref(tracking))
        # Qt 无边框分支可能吞掉非客户区点击。显式进入原生拖动/缩放/按钮处理，
        # Explorer 才能识别完整的系统移动循环并弹出屏幕顶部布局栏。
        if msg.message in (WM_NCLBUTTONDOWN, WM_NCLBUTTONUP, WM_NCLBUTTONDBLCLK, 0x00A5):
            return True, self.user32.DefWindowProcW(msg.hWnd, msg.message, msg.wParam, msg.lParam)
        # 系统负责非客户区的悬停菜单；不要再向 Qt 按钮合成一次点击。
        if 0x00A0 <= msg.message <= 0x00AD or msg.message == WM_NCMOUSELEAVE:
            result = ctypes.c_ssize_t()
            if self.dwmapi.DwmDefWindowProc(msg.hWnd, msg.message, msg.wParam, msg.lParam, ctypes.byref(result)):
                return True, result.value
        return False, 0
