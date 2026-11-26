"""后台轮询线程: 在独立 QThread 内跑 asyncio 事件循环, 通过 Qt 信号投递结果到 UI 线程。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Signal

from ..config import Config
from .client import WTClient, detect_port
from .mission import extract_scene_name
from .mapinfo import MapInfo
from .mapobj import parse_map_objects, MapObj
from .hudmsg import parse_hudmsg, HudEvent
from .telemetry import parse_indicators


log = logging.getLogger(__name__)

# 停止轮询的唤醒粒度(秒): 退出延迟上界, 同时是 stop() 后 wait() 的经验耗时上限
_STOP_TICK = 0.2
# 离线探活: 周期与单次超时。实测本机 127.0.0.1 无监听时一次 TCP 连接要约 2s 才失败,
# 若按 20Hz/10Hz/2Hz/1Hz 对死端口扇出请求, stop() 要到 ~10s 后才能退出, 故离线时降级探活。
_PROBE_PERIOD = 1.0
_PROBE_TIMEOUT = 0.4


def _pseudo_scene(mi: MapInfo) -> str | None:
    if not mi.valid:
        return None
    sig = ",".join(_quantize(v) for v in (*mi.grid_zero, *mi.grid_size))
    h = hashlib.md5(sig.encode()).hexdigest()[:8]
    return f"map_{h}"


def _quantize(v: float) -> str:
    """量化到 2 位有效数字。

    实测同一张图跨局网格会漂(131072.0 vs 131144.09375, 约 0.05%): 逐位哈希会让同一张图
    拿到不同伪场景名, 标点方案就再也调不出来。2 位有效数字既吃掉漂移, 又仍能区分
    尺寸不同的地图(如 1850 vs 131072)。
    """
    return f"{float(v):.2g}"


def map_img_due(prev_gen: int | None, new_gen: int | None, have_img: bool,
                sig_changed: bool = False) -> bool:
    """是否该下载 /map.img。

    游戏自带前端的官方做法: /map.img 是 512² 的 JPEG(实测单次 375ms/347KB), 只有在
    map_info.map_generation 变化时才重新取; 服务端串行处理, 盲拉会把 /state、/mission.json
    拖到超时。还没拿到底图时按缺图处理(重试)。

    `sig_changed` = **地图几何签名变了**(空中大图 ↔ 地面小图是两张不同的图): 这时即使 gen 没变
    也必须重取。真机现象: 陆战里先开飞机拿到空中大图, 下次出击换回地面载具时 gen 不变,
    只按 gen 判断就一直停在大地图上回不去。
    """
    if sig_changed or not have_img:
        return True
    if new_gen is None:
        return False
    return prev_gen != new_gen


MIN_BASE_PX = 512     # 底图最小边长(真图实测 2048²/4096²; 缓存里混进过 4 张 128×128 的失败垃圾)


def base_img_ok(img) -> bool:
    """底图可用性校验: 空图 / 尺寸异常一律丢弃, 绝不缓存。

    128² 那种失败缓存一旦被当成底图, 归一化坐标照样映射到整图 ⇒ 地图被放大得面目全非,
    而且会被 ensure_base_cache 写进 data/maps/<scene>/base.png, 那张地图长期被污染。
    """
    try:
        if img is None or img.isNull():
            return False
        return img.width() >= MIN_BASE_PX and img.height() >= MIN_BASE_PX
    except Exception:
        return False


def dump_diagnostic(data_root: Path | str, scene: str | None,
                    mission: Any, map_info: Any) -> None:
    """把 /mission.json 与 /map_info.json 原文落一份到 data/diagnostic/, 用于确认真实地图名键名。

    只在场景判定发生变化时调用(每局每图一次), 失败必须静默: 诊断落盘不得影响主流程。
    """
    try:
        d = Path(data_root) / "diagnostic"
        d.mkdir(parents=True, exist_ok=True)
        payload = {
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "scene_used": scene,
            "mission": mission,
            "map_info": map_info,
        }
        (d / "last_capture.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        log.debug("诊断落盘失败(忽略)", exc_info=True)


class Poller(QThread):
    connection_changed = Signal(bool)
    state_changed = Signal(dict)              # 原始 /state 全字段
    indicators_changed = Signal(object)       # 解析后的 Attitude(符号已按真机标定翻正) | None
    battle_state_changed = Signal(bool)       # map_info.valid 翻转: True=在对局里 False=离开/回大厅
    mission_status_changed = Signal(str)      # /mission.json 的总体状态（包括结算时的 success/fail）
    map_changed = Signal(object, object)      # (QImage, scene_name)
    map_info_changed = Signal(object)         # MapInfo
    scene_changed = Signal(object)            # scene_name
    map_objects_changed = Signal(list)        # list[MapObj]
    hud_events_changed = Signal(list)         # list[HudEvent] (增量)

    def __init__(self, cfg: Config, data_root, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.data_root = data_root
        # 停止标志必须是线程安全对象: 早先用 asyncio.Event 从 UI 线程 set(),
        # 唤醒的是别的线程里的 future(未定义行为), 轮询循环收不到 -> 退出时
        # QThread 仍在跑, Qt 报 "Destroyed while thread is still running" 并 fast-fail。
        # 现在用 threading.Event, 由循环每 _STOP_TICK 秒自查一次。
        self._stop = threading.Event()
        self._port = detect_port(cfg.config_blk_path, cfg.port)

    def stop(self) -> None:
        """可从任意线程调用, 包括 start() 之前。"""
        self._stop.set()

    async def _poll_loop(self) -> None:
        from PySide6.QtGui import QImage
        from .mapimg import ensure_base_cache

        online = False
        last_scene: str | None = None
        last_map_info: MapInfo | None = None
        seen_event_ids: set[int] = set()
        dumped_once = False
        # 底图按 map_generation 取; mi_raw 缓存下来给场景判定复用, 不重复请求
        last_gen: int | None = None
        last_valid: bool | None = None      # map_info.valid 翻转 => 进/出战场(一局结束)的信号
        last_mission_status = ""
        last_sig: str | None = None         # 地图几何签名(空中↔地面是两张图), 变了就强制重取底图
        have_img = False
        map_ready = False                  # 对象必须归属到已确认的底图，换图下载失败时先等
        mi_raw: dict[str, Any] = {}
        # /hudmsg 是双游标增量接口(裸 GET 返回 400)
        last_evt_id = 0
        last_dmg_id = 0

        async with WTClient(self.cfg.host, self._port) as client:
            period_state = 1.0 / max(self.cfg.poll.state, 1)
            period_map = 1.0 / max(self.cfg.poll.map_img, 1)   # 这里是"核对 map_generation"的节奏
            period_mission = 1.0 / max(self.cfg.poll.mission, 1)
            period_mission_idle = 1.0 / max(self.cfg.poll.mission_idle, 1) if self.cfg.poll.mission_idle > 0 else 60.0
            period_objs = 1.0 / 5.0   # /map_obj.json 5Hz: 官方前端整体只有 2Hz, 服务端串行, 别贪
            period_hud = 1.0 / 2.0    # /hudmsg 2Hz(带游标)
            period_ind = 1.0 / max(float(self.cfg.poll.indicators), 0.5)   # /indicators 姿态仪

            next_state = 0.0
            next_map = 0.0
            next_objs = 0.0
            next_hud = 0.0
            next_mission = 0.0
            next_probe = 0.0
            next_ind = 0.0
            cur_mission_period = period_mission

            while not self._stop.is_set():
                now = time.monotonic()

                # 离线降级: 只按 1Hz 发一次短超时探活, 不要对死端口扇出 5 路 2s 超时请求
                # (否则 stop() 要等一整轮才退出, 见 _PROBE_* 注释)。
                if not online:
                    if now < next_probe:
                        await asyncio.sleep(min(_STOP_TICK, next_probe - now))
                        continue
                    next_probe = now + _PROBE_PERIOD
                    try:
                        st = await client.fetch_state(timeout=_PROBE_TIMEOUT)
                    except Exception:
                        continue
                    online = True
                    # 新会话的 HUD 游标归零；探活响应也是首帧遥测，直接交给界面。
                    last_evt_id = last_dmg_id = 0
                    seen_event_ids.clear()
                    last_mission_status = ""
                    self.connection_changed.emit(True)
                    if st:
                        self.state_changed.emit(st)
                    next_state = now + period_state
                    next_ind = next_map = next_objs = next_hud = next_mission = now
                    cur_mission_period = period_mission

                # /state 高频
                if now >= next_state:
                    next_state = now + period_state
                    try:
                        st = await client.fetch_state()
                        if st:
                            self.state_changed.emit(st)
                    except Exception:
                        online = False
                        have_img = False       # 掉线/重开后底图与 gen 都不能信, 下一轮重新取
                        map_ready = False
                        last_gen = None
                        self.connection_changed.emit(False)
                        self.map_objects_changed.emit([])
                        continue                  # 掉线后马上回到短超时探活，不继续扇出请求

                # /indicators 姿态仪数据源(~1.1KB): 解析后发 Attitude, 符号按 config.attitude 翻正
                if self._stop.is_set():
                    break
                if now >= next_ind:
                    next_ind = now + period_ind
                    try:
                        at = self.cfg.attitude
                        self.indicators_changed.emit(
                            parse_indicators(await client.fetch_indicators(),
                                             invert_pitch=at.invert_pitch, invert_roll=at.invert_roll))
                    except Exception:
                        self.indicators_changed.emit(None)

                # 中途止血: 各请求本身带超时, 越早发现停止信号退出越快
                if self._stop.is_set():
                    break

                # /map_info.json 核对 gen -> 仅在 gen 变化(或缺图)时才下 /map.img?gen=N
                if now >= next_map:
                    next_map = now + period_map
                    map_ready = False
                    try:
                        mi_raw = await client.fetch_map_info()
                        mi = MapInfo.from_dict(mi_raw)
                        # 每轮都报 valid: 主窗口要判断"持续 false 多久"才能区分真离开与瞬时抖动
                        self.battle_state_changed.emit(bool(mi.valid))
                        last_valid = bool(mi.valid)
                        if mi.valid and mi != last_map_info:
                            last_map_info = mi
                            self.map_info_changed.emit(mi)
                        gen_raw = mi_raw.get("map_generation")
                        gen = int(gen_raw) if isinstance(gen_raw, (int, float)) else None
                        # 几何签名变化 = 空中大图 ↔ 地面小图 换了张图, gen 不变也得重取(修 K-4)
                        sig = _pseudo_scene(mi) if mi.valid else None
                        sig_changed = sig is not None and last_sig is not None and sig != last_sig
                        if not mi.valid:
                            have_img = False
                        if mi.valid and map_img_due(last_gen, gen, have_img, sig_changed=sig_changed):
                            data = await client.fetch_map_img_bytes(gen)
                            if data:
                                img = QImage()
                                img.loadFromData(data)
                                if base_img_ok(img):
                                    have_img = True
                                    # 用**实际**缓存目录名当场景名: 伪场景名是几何哈希会撞车
                                    # (两张不同的图算出同一个 map_xxxx), 那种情况下
                                    # ensure_base_cache 会另存成 <scene>-2 —— 底图与方案都必须
                                    # 跟着那个真名走, 否则两张图的标点会串在一起。
                                    actual = sig or last_scene
                                    if actual:
                                        actual = ensure_base_cache(
                                            self.data_root, actual, data).parent.name
                                        self.scene_changed.emit(actual)
                                    self.map_changed.emit(img, actual)
                                    last_gen = gen
                                    last_sig = sig  # 只确认成功的几何，下载失败时下一轮必须重试
                                else:
                                    log.warning("底图尺寸异常(%sx%s), 丢弃且不缓存",
                                                img.width(), img.height())
                                    # 记下被拒原因: 这类图**每次都会**被拒 ⇒ 永远没有底图, 地图库里
                                    # 只会看到网格画布。用户 2026-10-13 报的"空白地图"就是它。
                                    # 失败只记日志，禁止为了记拒绝原因创建无底图目录。
                        map_ready = bool(mi.valid and have_img and last_sig == sig and last_gen == gen)
                    except Exception:
                        log.debug("map_info/map.img 拉取失败", exc_info=True)

                # 先确认底图，再送这一轮的对象；否则起飞时的跑道会写到上一张陆战小图。
                if self._stop.is_set():
                    break
                if now >= next_objs:
                    next_objs = now + period_objs
                    try:
                        objs = parse_map_objects(await client.fetch_map_objects()) if map_ready else []
                        self.map_objects_changed.emit(objs)
                    except Exception:
                        self.map_objects_changed.emit([])

                # /hudmsg 2Hz: 带 lastEvt/lastDmg 游标向服务端要增量
                if self._stop.is_set():
                    break
                if now >= next_hud:
                    next_hud = now + period_hud
                    try:
                        evs = parse_hudmsg(await client.fetch_hudmsg(last_evt=last_evt_id, last_dmg=last_dmg_id))
                        if evs:
                            last_evt_id = max([last_evt_id] + [e.id for e in evs if e.stream == "events"])
                            last_dmg_id = max([last_dmg_id] + [e.id for e in evs if e.stream == "damage"])
                        new = [e for e in evs if e.id and e.id not in seen_event_ids]
                        for e in new:
                            seen_event_ids.add(e.id)
                        # 防止集合无限增长: 仅保留最近 400 条
                        if len(seen_event_ids) > 400:
                            seen_event_ids = set(sorted(seen_event_ids)[-400:])
                        if new:
                            self.hud_events_changed.emit(new)
                    except Exception:
                        pass

                # /mission.json 低频(用上面缓存的 map_info 判场景, 不再重复请求)
                if self._stop.is_set():
                    break
                if now >= next_mission:
                    next_mission = now + cur_mission_period
                    try:
                        m = await client.fetch_mission()
                        status = str(m.get("status") or "").strip().lower() if isinstance(m, dict) else ""
                        if status and status != last_mission_status:
                            last_mission_status = status
                            self.mission_status_changed.emit(status)
                        scene = extract_scene_name(m, mi_raw)
                        if not scene and mi_raw:
                            scene = _pseudo_scene(MapInfo.from_dict(mi_raw))
                        # 场景判定变化(或首次拿到数据)时留一份原文, 便于确认真实地图名键名
                        if scene != last_scene or not dumped_once:
                            dumped_once = True
                            dump_diagnostic(self.data_root, scene, m, mi_raw)
                        if scene and scene != last_scene:
                            last_scene = scene
                            cur_mission_period = period_mission_idle
                            # 换图不清游标: 同一客户端会话里 hudmsg 的 id 单调递增, 清了会把整段缓冲重收一遍
                            # （重复面板行 + 重复告警）。游标与去重集只在「重连」时归零, 见上面探活分支。
                            # 只有底图经内容匹配确认后的真实缓存名才能发给 UI。
                            # 几何伪名不能覆盖已经识别出的人工命名地图。
                    except Exception:
                        pass

                upcoming = [v for v in (next_state, next_ind, next_objs, next_map, next_hud, next_mission) if v > 0]
                sleep_for = max(0.0, min(upcoming) - time.monotonic()) if upcoming else 0.05
                # 分片短睡: 每片醒来自查停止标志, stop() 后最迟约 _STOP_TICK 秒退出循环
                await asyncio.sleep(min(sleep_for, _STOP_TICK))

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._poll_loop())
        except Exception:
            log.exception("轮询线程崩溃")
        finally:
            loop.close()
