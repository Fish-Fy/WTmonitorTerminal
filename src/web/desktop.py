"""桌面地图到手机的只读镜像；仅在开启后每秒采样两帧，不增加 8111 请求。"""
from __future__ import annotations

import ipaddress
import logging

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface

from ..i18n import language, map_name, tr
from ..agent.roster import STATUS_LABELS, short_name
from .broadcaster import LiveBus
from .server import MirrorServer

log = logging.getLogger(__name__)


def lan_addresses() -> list[str]:
    addresses = set()
    for interface in QNetworkInterface.allInterfaces():
        flags = interface.flags()
        if (not flags & QNetworkInterface.InterfaceFlag.IsUp
                or not flags & QNetworkInterface.InterfaceFlag.IsRunning
                or flags & QNetworkInterface.InterfaceFlag.IsLoopBack):
            continue
        for entry in interface.addressEntries():
            address = entry.ip()
            if address.protocol() != QAbstractSocket.NetworkLayerProtocol.IPv4Protocol:
                continue
            ip = ipaddress.ip_address(address.toString())
            if not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified:
                addresses.add(str(ip))
    return sorted(addresses, key=lambda value: (not value.startswith("192.168."),
                                                not value.startswith("10."), value))


class PhoneMirror(QObject):
    changed = Signal()

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.bus = LiveBus()
        self.server: MirrorServer | None = None
        self.error = ""
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.capture)

    @property
    def running(self) -> bool:
        return self.server is not None

    @property
    def local_url(self) -> str:
        if not self.server:
            return ""
        host = "127.0.0.1" if self.server.host in ("0.0.0.0", "localhost") else self.server.host
        return f"http://{host}:{self.server.bound_port}/"

    def urls(self) -> list[str]:
        if not self.server:
            return []
        host = self.server.host
        if host == "0.0.0.0":
            addresses = lan_addresses()
        elif host in ("127.0.0.1", "localhost"):
            addresses = []
        else:
            addresses = [host]
        return [f"http://{address}:{self.server.bound_port}/" for address in addresses]

    def start(self, port: int | None = None) -> bool:
        if self.running:
            return True
        cfg = self.window.cfg.web
        self.bus = LiveBus()
        server = MirrorServer(self.bus, host=cfg.host, port=cfg.port if port is None else port,
                              locale=language())
        try:
            server.start()
        except OSError as exc:
            self.error = tr('手机显示未能启动，端口可能已被占用：{0}', exc)
            self.changed.emit()
            return False
        self.server = server
        self.error = ""
        self.capture()
        self.timer.start()
        self.changed.emit()
        return True

    def stop(self) -> None:
        self.timer.stop()
        if self.server is not None:
            self.server.stop()
            self.server = None
        self.changed.emit()

    def roster_summary(self) -> dict:
        roster = self.window.roster
        result = {side: [] for side in ("me", "friend", "enemy", "unknown")}
        for name in roster.display_names():
            side = roster.side_of(name)
            stats = roster.stats(name)
            status = stats.get("status", "unknown")
            result.get(side, result["unknown"]).append({
                "name": name, "display_name": short_name(name),
                "kills": int(stats.get("kill_count") or 0),
                "vehicle": "?" if status == "killed" else stats.get("current_vehicle") or "?",
                "vehicles": [{"vehicle": vehicle, "kills": kills}
                             for vehicle, kills in roster.kills_detail(name)],
                "search_text": " ".join(roster.vehicles(name)),
                "status": tr(STATUS_LABELS.get(status, status)),
                "status_key": status,
                "status_time": float(stats.get("status_time") or 0),
                "is_me": side == "me",
                "highlight_killer": (self.window.roster_panel.highlight_killers()
                                     and side != "me" and bool(stats.get("killed_me"))),
                "killed_me": int(stats.get("killed_me") or 0),
            })
        order = {key: i for i, key in enumerate(("alive", "killed", "unknown", "left"))}
        for rows in result.values():
            rows.sort(key=lambda row: (order.get(row["status_key"], 2), -row["kills"], row["name"].casefold()))
        return result

    def capture(self) -> None:
        if not self.server:
            return
        try:
            window = self.window
            view = window.map_view
            pixmap = view.viewport().grab()
            # 手机无需桌面超宽屏的全部物理像素；等比例限制传输尺寸。
            if max(pixmap.width(), pixmap.height()) > 1400:
                from PySide6.QtCore import Qt
                pixmap = pixmap.scaled(1400, 1400, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not pixmap.save(buffer, "JPEG", 82):
                return
            self.bus.publish_frame(bytes(data), scene=window._scene,
                                   scene_label=map_name(window._scene) if window._scene else None,
                                   online=bool(getattr(window, "_online", False)),
                                   stale=bool(view._objs_stale or not view._last_objs or not getattr(window, "_online", False)),
                                   message=view.banner_text(),
                                   planning=bool(window._map_locked), width=pixmap.width(),
                                   height=pixmap.height(), has_map=view._base is not None,
                                   roster=self.roster_summary(),
                                   roster_options={
                                       "hide_left": window.roster_panel.alive_only(),
                                       "highlight_killers": window.roster_panel.highlight_killers(),
                                   })
        except Exception:
            log.exception("手机镜像采样失败")
