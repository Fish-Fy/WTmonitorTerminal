"""只读镜像的数据总线: MainWindow(UI 线程)推快照/事件, SSE 工作线程消费。

为什么是"快照 + 事件"双轨: 手机端中途打开页面, 必须先拿到**当前全量**(snapshot),
之后的增量变化走事件流(SSE)。快照在 bus 里就地维护, 主窗口不必管"新客户端要补什么"。

线程模型: 全部公开方法可被 UI 线程调用; 内部一把锁保护快照与订阅表;
订阅者使用有界队列 —— 慢客户端积压时丢旧帧，尽快回到最新画面，绝不阻塞 UI 线程。
"""
from __future__ import annotations

import queue
import copy
import threading
import time
from typing import Any

# 快照里交战速览保留几条(与悬浮窗 max_rows 思路一致: 防刷屏)
_COMBAT_KEEP = 6


class LiveBus:
    """只读镜像的现场总线。clock 可注入(测试)。"""

    def __init__(self, clock=None, obj_throttle: float = 0.5):
        self._clock = clock or time.monotonic
        self._obj_throttle = float(obj_throttle)      # 对象帧推送节流(秒), poller 5Hz → 镜像 2Hz
        self._last_obj_push = 0.0
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._map_png: bytes | None = None
        self._frame: bytes | None = None
        self._snap: dict[str, Any] = {
            "online": False,
            "scene": None, "scene_label": None, "has_map": False,
            "objects": [],
            "roster": {"me": [], "friend": [], "enemy": [], "unknown": []},
            "roster_options": {"hide_left": False, "highlight_killers": False},
            "banner": "",
            "combat": [],                              # [{"text","mine"}, ...] 新→旧
            "at": 0.0,
            "frame_rev": 0, "frame_width": 0, "frame_height": 0,
            "stale": True, "message": "等待游戏地图", "planning": False,
            "banner_at": 0.0,
        }

    # ------------------------------------------------------------------ 订阅
    def subscribe(self) -> queue.Queue:
        """SSE 工作线程注册一个事件队列; 返回的队列收到 dict(kind=..., ...)。"""
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            if len(self._subs) >= 16:
                raise OverflowError("手机连接数已达上限")
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        msg = {"kind": kind, **payload}
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass

    # ------------------------------------------------------------------ 快照读取
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._snap)

    def frame(self) -> bytes | None:
        with self._lock:
            return self._frame

    def publish_frame(self, jpeg: bytes, *, scene, online: bool, stale: bool,
                      message: str, planning: bool, width: int, height: int,
                      has_map: bool, roster: dict, roster_options: dict | None = None,
                      scene_label: str | None = None) -> None:
        """直接镜像 Qt 绘制结果，标点、轨迹、缓存提示与桌面一致。"""
        with self._lock:
            self._frame = jpeg
            self._snap.update(scene=scene, scene_label=scene_label, online=bool(online), stale=bool(stale),
                              message=message, planning=bool(planning), has_map=bool(has_map),
                              frame_width=width, frame_height=height, roster=copy.deepcopy(roster),
                              at=self._clock(), frame_rev=self._snap["frame_rev"] + 1)
            if roster_options is not None:
                self._snap["roster_options"] = copy.deepcopy(roster_options)
            snapshot = copy.deepcopy(self._snap)
        self._emit("frame", snapshot)

    def map_png(self) -> bytes | None:
        with self._lock:
            return self._map_png

    # ------------------------------------------------------------------ 主窗口写入(UI 线程)
    def set_online(self, online: bool) -> None:
        with self._lock:
            self._snap["online"] = bool(online)
            self._snap["at"] = self._clock()
        self._emit("online", {"online": bool(online)})

    def set_scene(self, scene: str | None) -> None:
        with self._lock:
            self._snap["scene"] = scene
            self._snap["scene_label"] = None
            self._snap["at"] = self._clock()
        self._emit("scene", {"scene": scene})

    def set_map(self, png_bytes: bytes | None) -> None:
        with self._lock:
            self._map_png = png_bytes
            self._snap["has_map"] = bool(png_bytes)
            self._snap["at"] = self._clock()
        self._emit("map", {"has_map": bool(png_bytes)})

    def push_objects(self, objects: list[dict[str, Any]]) -> bool:
        """对象帧(已序列化)。2Hz 节流: 窗口内的帧直接丢(下一帧很快来), 返回是否真推。"""
        now = self._clock()
        if now - self._last_obj_push < self._obj_throttle:
            return False
        self._last_obj_push = now
        with self._lock:
            self._snap["objects"] = list(objects)
            self._snap["at"] = now
        self._emit("objects", {"objects": objects, "at": now})
        return True

    def push_roster(self, summary: dict[str, Any]) -> None:
        with self._lock:
            self._snap["roster"] = summary
            self._snap["at"] = self._clock()
        self._emit("roster", {"roster": summary})

    def push_threat(self, msg: str) -> None:
        """威胁告警横幅(附近连死/连杀绕后): 快照留最新一条, 事件立刻广播。"""
        with self._lock:
            self._snap["banner"] = msg
            self._snap["banner_at"] = time.time()
            self._snap["at"] = self._clock()
            at = self._snap["banner_at"]
        self._emit("threat", {"banner": msg, "banner_at": at})

    def push_combat(self, text: str, mine: bool) -> None:
        with self._lock:
            rows = [{"text": text, "mine": bool(mine)}] + self._snap["combat"]
            self._snap["combat"] = rows[:_COMBAT_KEEP]
            self._snap["at"] = self._clock()
        self._emit("combat", {"text": text, "mine": bool(mine)})

    # ------------------------------------------------------------------ 对局边界
    def reset_battle(self, reason: str = "") -> None:
        """随 _fresh_roster/_on_new_battle 清空: 镜像端看到的也必须从零开始(识别不跨局)。"""
        with self._lock:
            self._snap["objects"] = []
            self._snap["roster"] = {"me": [], "friend": [], "enemy": [], "unknown": []}
            self._snap["banner"] = ""
            self._snap["banner_at"] = 0.0
            self._snap["combat"] = []
            self._snap["at"] = self._clock()
        self._emit("reset", {"reason": reason})
