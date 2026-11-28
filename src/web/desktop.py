"""只读手机地图画面，手机和电脑可以选择不同底图。"""
from __future__ import annotations

import ipaddress
import logging
from dataclasses import replace

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, Qt, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface

from ..i18n import language, map_name, tr
from ..agent.roster import STATUS_LABELS, short_name
from ..agent.live_maps import LIVE_FRAME_SEC
from ..store.markers import load_schemes
from ..ui.map_view import MapView
from .broadcaster import LiveBus
from .server import MirrorServer

log = logging.getLogger(__name__)
_FRAME_INTERVAL_MS = 200


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
        self._map_view = None
        self._selected_map = None
        self._painted_objects = None
        self._painted_settings = None
        self._battle_maps = None
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(_FRAME_INTERVAL_MS)
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
        if self._map_view is not None:
            self._map_view.close()
            self._map_view.deleteLater()
            self._map_view = self._selected_map = self._painted_objects = self._painted_settings = None
            self._battle_maps = None
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

    def _select_view(self):
        window = self.window
        desktop = window.map_view
        maps = window._live_maps
        mode = window.ui_prefs.phone_map_mode
        if mode == "follow" and (window._map_locked or
                (maps.current is None and window.ui_prefs.desktop_map_mode == "follow")):
            return desktop, window._scene, window._map_locked
        target = maps.select(mode, window._veh_kind)
        if (target is not None and not window._map_locked
                and target is window._desktop_live_map and target.scene == window._scene):
            return desktop, target.scene, False
        if self._map_view is None:
            self._map_view = MapView(window)
            self._map_view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            self._map_view.resize(720, 720)
            self._map_view.show()
        view = self._map_view
        if target is None:
            if self._selected_map is not None and self._battle_maps is maps:
                self._selected_map.player_trails["phone"] = view.player_trail_snapshot()
            self._selected_map = self._painted_objects = None
            view.overlay.set_flight_active(False)
            view.set_scene_name(None)
            if view._scheme is not None:
                view.set_scheme(None)
            view.show_waiting_for_map(tr("等待本局所选地图 · 可在游戏内切图一次"))
            return view, None, False

        changed = self._selected_map is not target or self._battle_maps is not maps
        if changed:
            if self._selected_map is not None and self._battle_maps is maps:
                self._selected_map.player_trails["phone"] = view.player_trail_snapshot()
            self._selected_map = target
            self._battle_maps = maps
            self._painted_objects = None
            view.on_reconnect()
            view.set_scene_name(target.scene)
            view.load_image(target.image, new_scene=True)
            view.show_whole_map()
            view.restore_player_trail(target.player_trails.get("phone"))
            schemes = load_schemes(window.data_root, target.scene)
            task_ids = target.task.ids if target.task else set()
            scheme = next((item for item in schemes if task_ids.intersection(item.task_ids)), None)
            view.set_scheme(scheme or next((item for item in schemes if not item.task_ids), None))

        settings = replace(desktop.settings, follow_player=False)
        options_changed = settings != self._painted_settings
        if options_changed:
            self._painted_settings = settings
            view.settings = settings
        view.set_aircraft_active(window._veh_kind == "air")
        view.set_overlay_items(desktop.overlay_items())
        view.overlay.set_aircraft_tilt(desktop.overlay.aircraft_tilt)
        flight_active = window._veh_kind == "air"
        view.overlay.set_flight_active(flight_active)
        view.set_online(getattr(window, "_online", False))
        if flight_active and desktop.overlay._att is not view.overlay._att:
            view.set_attitude(desktop.overlay._att)
        if flight_active and desktop.overlay._st is not view.overlay._st:
            view.set_telemetry(desktop.overlay._st)

        now = window._clock()
        native = target is maps.current
        objects = window._game_objects if native else maps.checked_objects
        captured_at = window._game_objects_at if native else maps.checked_at
        if now - captured_at > LIVE_FRAME_SEC or (native and window._game_objects_map is not target):
            objects = None
        if changed or options_changed or objects is not self._painted_objects:
            record_tracks = objects is not self._painted_objects
            self._painted_objects = objects
            if objects:
                frame = objects if native else maps.projected(target, now, LIVE_FRAME_SEC)
                if frame == []:
                    view.clear_objects()
                else:
                    view.set_map_objects(frame or [], record_tracks=record_tracks)
            else:
                view.clear_objects()
                view.set_map_objects([], record_tracks=False)
        return view, target.scene, False

    def capture(self) -> None:
        if not self.server:
            return
        try:
            window = self.window
            view, scene, planning = self._select_view()
            pixmap = view.viewport().grab()
            # 手机无需桌面超宽屏的全部物理像素；等比例限制传输尺寸。
            if max(pixmap.width(), pixmap.height()) > 1400:
                pixmap = pixmap.scaled(1400, 1400, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
            data = QByteArray()
            buffer = QBuffer(data)
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if not pixmap.save(buffer, "JPEG", 82):
                return
            self.bus.publish_frame(bytes(data), scene=scene,
                                   scene_label=map_name(scene) if scene else None,
                                   online=bool(getattr(window, "_online", False)),
                                   stale=bool(view._objs_stale or not view._last_objs or not getattr(window, "_online", False)),
                                   message=view.banner_text(),
                                   planning=planning, width=pixmap.width(),
                                   height=pixmap.height(), has_map=view._base is not None,
                                   roster=self.roster_summary(),
                                   roster_options={
                                       "hide_left": window.roster_panel.alive_only(),
                                       "highlight_killers": window.roster_panel.highlight_killers(),
                                   })
        except Exception:
            log.exception("手机镜像采样失败")
